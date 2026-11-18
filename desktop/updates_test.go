package main

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Only this test transport trusts the disposable fixture certificate.
func updateFixture(t *testing.T, handler http.HandlerFunc) *http.Client {
	t.Helper()
	server := httptest.NewTLSServer(handler)
	t.Cleanup(server.Close)
	transport := &http.Transport{DialTLSContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&tls.Dialer{Config: &tls.Config{InsecureSkipVerify: true}}).DialContext(ctx, network, server.Listener.Addr().String())
	}}
	t.Cleanup(transport.CloseIdleConnections)
	return &http.Client{Transport: transport, Timeout: 5 * time.Second}
}
func fixtureUpdater(t *testing.T, client *http.Client) *AppUpdates {
	t.Helper()
	u := newAppUpdates(context.Background(), updateConfig{version: "1.2.0-rc.1", repository: "owner/forkrail", target: "macos-arm64", tempRoot: t.TempDir()}, client)
	u.authToken = func(context.Context) string { return "" }
	t.Cleanup(u.shutdown)
	return u
}
func releaseFixture(version string, draft, pre bool) map[string]any {
	return map[string]any{"tag_name": "v" + version, "draft": draft, "prerelease": pre, "assets": []any{
		map[string]any{"id": 1, "name": "macos-arm64-release-manifest.json", "size": 1},
		map[string]any{"id": 2, "name": "macos-arm64-SHA256SUMS", "size": 1},
		map[string]any{"id": 3, "name": "Forkrail-" + version + "-macos-arm64.pkg", "size": 8},
	}}
}
func TestUpdaterSelectsSemverAndRejectsDraftAndChannel(t *testing.T) {
	releases := []any{releaseFixture("9.0.0", true, false), releaseFixture("1.2.0-rc.2", false, true), releaseFixture("1.2.0-rc.10", false, true), releaseFixture("1.1.9", false, false)}
	client := updateFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/releases") {
			json.NewEncoder(w).Encode(releases)
		} else {
			fmt.Fprint(w, `{"private":false}`)
		}
	})
	u := fixtureUpdater(t, client)
	if got := u.Check(); got.Status != "available" || got.LatestVersion != "1.2.0-rc.10" {
		t.Fatalf("prerelease ordering: %+v", got)
	}
	u.config.version = "1.1.9"
	u.state.CurrentVersion = "1.1.9"
	if got := u.Check(); got.Status != "current" {
		t.Fatalf("stable must not offer prerelease: %+v", got)
	}
}
func TestUpdaterDistinguishesInaccessibleRepositoryAndNoRelease(t *testing.T) {
	for _, status := range []int{200, 401, 404, 500} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			client := updateFixture(t, func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
				if strings.HasSuffix(r.URL.Path, "/releases") {
					fmt.Fprint(w, `[]`)
				} else {
					fmt.Fprint(w, `{}`)
				}
			})
			u := fixtureUpdater(t, client)
			want := map[int]string{200: "unpublished", 401: "auth_required", 404: "auth_required", 500: "error"}[status]
			if got := u.Check(); got.Status != want {
				t.Fatalf("want %s: %+v", want, got)
			}
		})
	}
}
func TestUpdaterDownloadVerifiesBeforeReadyAndInstall(t *testing.T) {
	for _, bad := range []string{"", "hash", "version", "target", "name", "size", "unsigned-stable", "symlink"} {
		t.Run(bad, func(t *testing.T) {
			version := "1.2.0-rc.2"
			if bad == "unsigned-stable" {
				version = "1.2.0"
			}
			name := "Forkrail-" + version + "-macos-arm64.pkg"
			payload := []byte("xar!test")
			manifest := map[string]any{"version": version, "target": "macos-arm64", "signed": false, "source_commit": strings.Repeat("a", 40), "source_dirty": false, "artifacts": []string{name, strings.TrimSuffix(name, ".pkg") + ".zip"}}
			switch bad {
			case "version":
				manifest["version"] = "1.2.0-rc.1"
			case "target":
				manifest["target"] = "windows-x64"
			case "name":
				manifest["artifacts"] = []string{"../evil.pkg", name}
			}
			manifestBytes, _ := json.Marshal(manifest)
			hash := fmt.Sprintf("%x", sha256.Sum256(payload))
			if bad == "hash" {
				hash = strings.Repeat("0", 64)
			}
			sums := fmt.Sprintf("%x  macos-arm64-release-manifest.json\n%s  %s\n", sha256.Sum256(manifestBytes), hash, name)
			release := releaseFixture(version, false, strings.Contains(version, "-"))
			assets := release["assets"].([]any)
			assets[0].(map[string]any)["size"] = len(manifestBytes)
			assets[1].(map[string]any)["size"] = len(sums)
			if bad == "size" {
				assets[2].(map[string]any)["size"] = 7
			}
			client := updateFixture(t, func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/repos/owner/forkrail/releases":
					json.NewEncoder(w).Encode([]any{release})
				case "/repos/owner/forkrail/releases/assets/1":
					w.Write(manifestBytes)
				case "/repos/owner/forkrail/releases/assets/2":
					fmt.Fprint(w, sums)
				case "/repos/owner/forkrail/releases/assets/3":
					w.Write(payload)
				default:
					fmt.Fprint(w, `{}`)
				}
			})
			u := fixtureUpdater(t, client)
			u.Check()
			got := u.Download()
			if bad != "" && bad != "symlink" {
				if got.Status != "error" {
					t.Fatalf("unsafe artifact accepted: %+v", got)
				}
				return
			}
			if got.Status != "ready" || got.DownloadedBytes != 8 || got.Signed {
				t.Fatalf("not ready: %+v", got)
			}
			var launched atomic.Int32
			u.verifySignature = func(context.Context, string, bool) (bool, error) { return false, nil }
			u.confirmInstall = func(UpdateState) (bool, error) { return false, nil }
			u.launchInstaller = func(path string) error {
				if filepath.Base(path) != name {
					t.Errorf("wrong path %s", path)
				}
				launched.Add(1)
				return nil
			}
			if got := u.Install(); got.Status != "ready" || launched.Load() != 0 {
				t.Fatalf("cancellation: %+v", got)
			}
			u.confirmInstall = func(UpdateState) (bool, error) { return true, nil }
			if bad == "symlink" {
				original := u.installerPath
				replacement := filepath.Join(t.TempDir(), "external.pkg")
				os.WriteFile(replacement, payload, 0600)
				os.Remove(original)
				os.Symlink(replacement, original)
				if got := u.Install(); got.Status != "error" || launched.Load() != 0 {
					t.Fatalf("symlink installed: %+v", got)
				}
				return
			}
			if got := u.Install(); got.Status != "installing" || launched.Load() != 1 {
				t.Fatalf("install: %+v", got)
			}
			u.Install()
			if launched.Load() != 1 {
				t.Fatal("duplicate installer launch")
			}
		})
	}
}
func TestUpdateRedirectNeverForwardsAuthentication(t *testing.T) {
	var leaked atomic.Bool
	client := updateFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Host == "api.github.com" {
			if r.Header.Get("Authorization") != "Bearer test-secret" {
				t.Error("API auth missing")
			}
			http.Redirect(w, r, "https://release-assets.githubusercontent.com/installer", 302)
			return
		}
		if r.Header.Get("Authorization") != "" {
			leaked.Store(true)
		}
		fmt.Fprint(w, "verified")
	})
	u := fixtureUpdater(t, client)
	body, err := u.fetchSmall(context.Background(), "https://api.github.com/repos/owner/forkrail/releases/assets/1", "test-secret", true, 100)
	if err != nil || string(body) != "verified" || leaked.Load() {
		t.Fatalf("redirect safety: %q %v leaked=%v", body, err, leaked.Load())
	}
}
func TestUpdateRejectsUnsafeRedirect(t *testing.T) {
	for _, location := range []string{"http://release-assets.githubusercontent.com/file", "https://attacker.example/file", "https://api.github.com.evil.test/file", "https://user@github.com/file", "https://github.com:444/file"} {
		t.Run(location, func(t *testing.T) {
			var count atomic.Int32
			u := fixtureUpdater(t, updateFixture(t, func(w http.ResponseWriter, r *http.Request) { count.Add(1); http.Redirect(w, r, location, 302) }))
			_, err := u.fetchSmall(context.Background(), "https://api.github.com/repos/owner/forkrail/releases/assets/1", "secret", true, 100)
			if err == nil || count.Load() != 1 {
				t.Fatalf("unsafe redirect attempted: %v requests=%d", err, count.Load())
			}
		})
	}
}
func TestUpdateVersionValidation(t *testing.T) {
	for _, value := range []string{"01.2.3", "1.2.3-rc.01", "1.2.3-rc.0/evil", "1.2.3+../../evil", "1.2.3\n", "999999999999999999999999.2.3", "v1.2.3", "1.2", "1.2.3-foo.1"} {
		if _, err := parseUpdateVersion(value); err == nil {
			t.Errorf("accepted %q", value)
		}
	}
}

func validDownloadFixture(t *testing.T, stream func(http.ResponseWriter, *http.Request)) *AppUpdates {
	t.Helper()
	name := "Forkrail-1.2.0-rc.2-macos-arm64.pkg"
	manifest := []byte(`{"version":"1.2.0-rc.2","target":"macos-arm64","signed":false,"source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_dirty":false,"artifacts":["Forkrail-1.2.0-rc.2-macos-arm64.pkg","Forkrail-1.2.0-rc.2-macos-arm64.zip"]}`)
	sums := fmt.Sprintf("%x  macos-arm64-release-manifest.json\n%x  %s\n", sha256.Sum256(manifest), sha256.Sum256([]byte("xar!test")), name)
	release := releaseFixture("1.2.0-rc.2", false, true)
	assets := release["assets"].([]any)
	assets[0].(map[string]any)["size"] = len(manifest)
	assets[1].(map[string]any)["size"] = len(sums)
	return fixtureUpdater(t, updateFixture(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/repos/owner/forkrail/releases":
			json.NewEncoder(w).Encode([]any{release})
		case "/repos/owner/forkrail/releases/assets/1":
			w.Write(manifest)
		case "/repos/owner/forkrail/releases/assets/2":
			fmt.Fprint(w, sums)
		case "/repos/owner/forkrail/releases/assets/3":
			stream(w, r)
		default:
			fmt.Fprint(w, `{}`)
		}
	}))
}
func TestUpdateSerializesOperationsAndCleansCancelledDownload(t *testing.T) {
	started := make(chan struct{})
	var count atomic.Int32
	u := validDownloadFixture(t, func(w http.ResponseWriter, r *http.Request) {
		count.Add(1)
		fmt.Fprint(w, "xar!")
		w.(http.Flusher).Flush()
		close(started)
		<-r.Context().Done()
	})
	if got := u.Check(); got.Status != "available" {
		t.Fatal(got)
	}
	result := make(chan UpdateState, 1)
	go func() { result <- u.Download() }()
	<-started
	for _, operation := range []func() UpdateState{u.Check, u.Download, u.Install} {
		if got := operation(); got.Status != "downloading" {
			t.Fatal(got)
		}
	}
	deadline := time.Now().Add(time.Second)
	for u.Info().DownloadedBytes < 4 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if u.Info().DownloadedBytes != 4 {
		t.Fatal("streaming progress unavailable")
	}
	u.shutdown()
	got := <-result
	if got.Status != "error" || count.Load() != 1 {
		t.Fatalf("cancel/serialize: %+v count=%d", got, count.Load())
	}
	files, err := os.ReadDir(u.config.tempRoot)
	if err != nil || len(files) != 0 {
		t.Fatalf("partial downloads left behind: %v %v", files, err)
	}
}
func TestUpdateInstallRechecksAfterConfirmationAndRetriesLaunch(t *testing.T) {
	u := validDownloadFixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "xar!test") })
	u.Check()
	if got := u.Download(); got.Status != "ready" {
		t.Fatal(got)
	}
	u.verifySignature = func(context.Context, string, bool) (bool, error) { return false, nil }
	u.confirmInstall = func(UpdateState) (bool, error) { return true, nil }
	u.launchInstaller = func(string) error { return fmt.Errorf("secret /Users/person/path") }
	got := u.Install()
	if got.Status != "ready" || strings.Contains(got.Message, "secret") {
		t.Fatal(got)
	}
	var launches atomic.Int32
	u.launchInstaller = func(string) error { launches.Add(1); return nil }
	u.confirmInstall = func(UpdateState) (bool, error) {
		os.WriteFile(u.installerPath, []byte("xar!evil"), 0600)
		return true, nil
	}
	if got := u.Install(); got.Status != "error" || launches.Load() != 0 {
		t.Fatalf("tampered after dialog: %+v", got)
	}
}
func TestUpdateRejectsInvalidAssetsAndOversizeMetadata(t *testing.T) {
	for _, kind := range []string{"missing", "duplicate", "path", "zero-id", "too-large", "metadata-too-large"} {
		t.Run(kind, func(t *testing.T) {
			release := releaseFixture("1.2.0-rc.2", false, true)
			assets := release["assets"].([]any)
			switch kind {
			case "missing":
				assets = assets[:2]
			case "duplicate":
				assets = append(assets, assets[0])
			case "path":
				assets[0].(map[string]any)["name"] = "../manifest.json"
			case "zero-id":
				assets[0].(map[string]any)["id"] = 0
			case "too-large":
				assets[2].(map[string]any)["size"] = int64(1<<30) + 1
			case "metadata-too-large":
				assets[0].(map[string]any)["size"] = 65537
			}
			release["assets"] = assets
			u := fixtureUpdater(t, updateFixture(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/releases") {
					json.NewEncoder(w).Encode([]any{release})
				} else {
					fmt.Fprint(w, `{}`)
				}
			}))
			if got := u.Check(); got.Status != "error" {
				t.Fatalf("unsafe asset accepted: %+v", got)
			}
		})
	}
}

func TestUpdateFallsBackToAnonymousForPublicRepositoryWithExpiredCredentials(t *testing.T) {
	var authenticated, anonymous atomic.Int32
	u := fixtureUpdater(t, updateFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			authenticated.Add(1)
			w.WriteHeader(401)
			fmt.Fprint(w, `{"message":"Bad credentials"}`)
			return
		}
		anonymous.Add(1)
		if strings.HasSuffix(r.URL.Path, "/releases") {
			fmt.Fprint(w, `[]`)
		} else {
			fmt.Fprint(w, `{"private":false}`)
		}
	}))
	u.authToken = func(context.Context) string { return "expired-secret" }
	if got := u.Check(); got.Status != "unpublished" {
		t.Fatalf("public repo blocked by stale CLI credentials: %+v", got)
	}
	if authenticated.Load() == 0 || anonymous.Load() == 0 {
		t.Fatal("authentication fallback was not exercised")
	}
}

func TestUpdateCannotBecomeReadyWithoutOSDownloadOrigin(t *testing.T) {
	u := validDownloadFixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "xar!test") })
	u.markOrigin = func(string, string) error { return fmt.Errorf("filesystem cannot preserve origin") }
	u.Check()
	got := u.Download()
	if got.Status != "error" {
		t.Fatalf("installer without OS security metadata became ready: %+v", got)
	}
	files, err := os.ReadDir(u.config.tempRoot)
	if err != nil || len(files) != 0 {
		t.Fatalf("unsafe download retained: %v %v", files, err)
	}
}
