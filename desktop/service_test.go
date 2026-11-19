package main

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

func fixtureConfig(t *testing.T) serviceConfig {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	port, err := freeLoopbackPort()
	if err != nil {
		t.Fatal(err)
	}
	return serviceConfig{
		port: port, executable: executable, dataDir: filepath.Join(t.TempDir(), "데이터 공백"),
		version: "1.2.0-rc.1", readinessTimeout: 5 * time.Second,
	}
}

func mockBoard(t *testing.T, health, state string, icon []byte) (*httptest.Server, int) {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, health) })
	mux.HandleFunc("/api/state", func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, state) })
	mux.HandleFunc("/assets/brand.png", func(w http.ResponseWriter, r *http.Request) { w.Write(icon) })
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	u, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil {
		t.Fatal(err)
	}
	return server, port
}

const legacyState = `{"now":1760000000.5,"stories":[],"story":"","items":[],"metrics":[],"timeline":[],"sessions":[]}`

func TestLegacyServiceMustMatchIconAndState(t *testing.T) {
	icon, err := os.ReadFile("../tests/fixtures/forkrail-legacy-icon.png")
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprintf("%x", sha256.Sum256(icon)); got != legacyIconSHA256 {
		t.Fatalf("legacy icon identity is stale: %s", got)
	}
	for _, test := range []struct {
		name, health, state string
		icon                []byte
		accept              bool
	}{
		{"same board", `{"ok":true}`, legacyState, icon, true},
		{"health only", `{"ok":true}`, `{}`, nil, false},
		{"unrelated icon", `{"ok":true}`, legacyState, []byte("other"), false},
		{"invalid state", `{"ok":true}`, `{"now":1,"stories":{},"story":"","items":[],"metrics":[],"timeline":[],"sessions":[]}`, icon, false},
		{"unsupported API", `{"ok":true,"app":"Forkrail","api":2,"version":"2.0.0"}`, legacyState, icon, false},
		{"branded impostor", `{"ok":true,"app":"Other","api":1}`, legacyState, icon, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			server, port := mockBoard(t, test.health, test.state, test.icon)
			cfg := fixtureConfig(t)
			cfg.port = port
			cfg.executable = filepath.Join(t.TempDir(), "unavailable")
			service, err := startService(context.Background(), cfg)
			if test.accept {
				if err != nil || service.owned != nil {
					t.Fatalf("expected reuse without process ownership, service=%v err=%v", service, err)
				}
				service.stop()
			} else if !errors.Is(err, errPortConflict) {
				t.Fatalf("expected port conflict, got %v", err)
			}
			response, err := server.Client().Get(server.URL + "/api/health")
			if err != nil {
				t.Fatalf("occupying service was stopped: %v", err)
			}
			response.Body.Close()
		})
	}
}

func TestOwnedServiceReceivesParentAndDataAndStops(t *testing.T) {
	cfg := fixtureConfig(t)
	service, err := startService(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(service.stop)
	if service.owned == nil {
		t.Fatal("expected an owned child")
	}
	data, err := os.ReadFile(filepath.Join(cfg.dataDir, "parent.txt"))
	if err != nil || string(data) != strconv.Itoa(os.Getpid()) {
		t.Fatalf("fixture received wrong parent or data path: %q, %v", data, err)
	}
	service.stop()
	select {
	case <-service.owned.done:
	default:
		t.Fatal("stop returned before the owned process exited")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := readHealth(ctx, service.target); err == nil {
		t.Fatal("owned service still responds after stop")
	}
}

func TestOwnedServiceRejectsWrongInstance(t *testing.T) {
	t.Setenv("FORKRAIL_FIXTURE_INSTANCE", "wrong")
	cfg := fixtureConfig(t)
	service, err := startService(context.Background(), cfg)
	if service != nil || !errors.Is(err, errUnexpectedHealth) {
		t.Fatalf("expected instance mismatch, got service=%v err=%v", service, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := readHealth(ctx, serviceURL(cfg.port)); err == nil {
		t.Fatal("service with a wrong instance is still running")
	}
}

func TestOwnedServiceStopsOnReadinessTimeout(t *testing.T) {
	t.Setenv("FORKRAIL_FIXTURE_INSTANCE", "unready")
	cfg := fixtureConfig(t)
	cfg.readinessTimeout = 350 * time.Millisecond
	start := time.Now()
	service, err := startService(context.Background(), cfg)
	if service != nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected readiness timeout, got service=%v err=%v", service, err)
	}
	if time.Since(start) > 4*time.Second {
		t.Fatal("launcher did not bound startup and cleanup time")
	}
}

func TestSmokeServiceVerifiesBundledBoard(t *testing.T) {
	if err := smokeService(context.Background(), fixtureConfig(t)); err != nil {
		t.Fatal(err)
	}
}

func TestServiceProxyAndPollerRejectALaterListener(t *testing.T) {
	if runtime.GOOS != "darwin" && runtime.GOOS != "windows" {
		t.Skip("desktop listener ownership is supported on macOS and Windows")
	}
	for _, name := range []string{"bundled", "reused"} {
		t.Run(name, func(t *testing.T) {
			ctx := context.Background()
			cfg := fixtureConfig(t)
			first, err := startService(ctx, cfg)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(first.stop)
			attached := first
			if name == "reused" {
				attached, err = startService(ctx, cfg)
				if err != nil || attached.owned != nil {
					t.Fatalf("expected to reuse the first process: %v", err)
				}
				t.Cleanup(attached.stop)
			}
			proxy := newBoardProxy(attached, "wails://wails")
			request := func() int {
				result := httptest.NewRecorder()
				proxy.ServeHTTP(result, httptest.NewRequest(http.MethodGet, "/api/health", nil))
				return result.Code
			}
			if status := request(); status != http.StatusOK {
				t.Fatalf("original listener status: %d", status)
			}
			if _, err := pollWaiting(ctx, attached); err != nil {
				t.Fatalf("original listener poll: %v", err)
			}

			// Simulate a service exit while the app still holds its original proxy.
			first.owned.stop()
			replacement, err := startService(ctx, cfg)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(replacement.stop)
			if status := request(); status != http.StatusBadGateway {
				t.Fatalf("proxy trusted a replacement process on the same port: HTTP %d", status)
			}
			if _, err := pollWaiting(ctx, attached); err == nil {
				t.Fatal("poller trusted a replacement process on the same port")
			}
			attached.stop()
			if _, err := readHealth(ctx, replacement.target); err != nil {
				t.Fatalf("stopping the original attachment affected the replacement: %v", err)
			}
		})
	}
}

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == "serve" {
		if err := runFixture(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		return
	}
	os.Exit(m.Run())
}

func runFixture(args []string) error {
	if len(args) != 8 {
		return fmt.Errorf("expected four serve flags, got %d", len(args))
	}
	values := map[string]string{}
	for i := 0; i < len(args); i += 2 {
		values[args[i]] = args[i+1]
	}
	dir := values["--data-dir"]
	if !strings.HasSuffix(dir, "데이터 공백") {
		return fmt.Errorf("data path was not passed as one argument")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "parent.txt"), []byte(values["--parent-pid"]), 0600); err != nil {
		return err
	}
	instance := values["--instance"]
	if mode := os.Getenv("FORKRAIL_FIXTURE_INSTANCE"); mode != "" {
		instance = mode
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		if instance == "unready" {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		fmt.Fprintf(w, `{"ok":true,"app":"Forkrail","api":1,"version":"1.2.0-rc.1","instance":%q}`, instance)
	})
	mux.HandleFunc("/api/sessions", func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"sessions":[]}`)
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `<!doctype html><title>Huginn</title><main id="main"></main>`)
	})
	return http.ListenAndServe("127.0.0.1:"+values["--port"], mux)
}
