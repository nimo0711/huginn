package main

import (
	"context"
	"encoding/json"
	"errors"
	"golang.org/x/sys/unix"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestMacUpdateAcceptsOnlyExplicitlyUnsignedPrereleaseImage(t *testing.T) {
	path := filepath.Join(t.TempDir(), "서명 없는 업데이트.dmg")
	if err := os.WriteFile(path, dmgFixture(), 0600); err != nil {
		t.Fatal(err)
	}
	if signed, err := verifyInstallerSignature(context.Background(), path, false); err != nil || signed {
		t.Fatalf("unsigned prerelease rejected: signed=%v error=%v", signed, err)
	}
	if _, err := verifyInstallerSignature(context.Background(), path, true); err == nil {
		t.Fatal("required signature was absent")
	}
	if _, err := verifyInstallerSignature(context.Background(), path+" missing", false); err == nil {
		t.Fatal("missing image treated as unsigned")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := verifyInstallerSignature(ctx, path, false); err == nil {
		t.Fatal("cancelled verification treated as unsigned")
	}
}

func TestMacUpdateRejectsUntrustedAndTamperedSignatures(t *testing.T) {
	path := filepath.Join(t.TempDir(), "signature.dmg")
	if err := os.WriteFile(path, dmgFixture(), 0600); err != nil {
		t.Fatal(err)
	}
	// Ad-hoc signing needs no certificate or keychain changes. It distinguishes
	// a valid but untrusted signature from an actually unsigned prerelease.
	if output, err := exec.Command("/usr/bin/codesign", "--force", "--sign", "-", path).CombinedOutput(); err != nil {
		t.Fatalf("create disposable signature: %v %s", err, output)
	}
	if output, err := exec.Command("/usr/bin/codesign", "--verify", "--strict", path).CombinedOutput(); err != nil {
		t.Fatalf("fixture signature invalid before tampering: %v %s", err, output)
	}
	if _, err := verifyInstallerSignature(context.Background(), path, false); err == nil {
		t.Fatal("untrusted signature treated as unsigned prerelease")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	data[0] ^= 0xff
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := verifyInstallerSignature(context.Background(), path, false); err == nil {
		t.Fatal("tampered signature treated as unsigned prerelease")
	}
}

func TestMacUpdatePreservesQuarantineOnDownloadedInstaller(t *testing.T) {
	path := filepath.Join(t.TempDir(), "한글 설치 파일.dmg")
	if err := os.WriteFile(path, []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := markInstallerOrigin(path, "https://github.com/owner/huginn/releases/tag/v1.2.0-rc.2"); err != nil {
		t.Fatal(err)
	}
	data := make([]byte, 1024)
	n, err := unix.Getxattr(path, "com.apple.quarantine", data)
	if err != nil || !strings.HasPrefix(string(data[:n]), "0081;") || !strings.Contains(string(data[:n]), ";Huginn;") {
		t.Fatalf("missing OS quarantine: %q %v", data[:n], err)
	}
}

func TestMacUpdateCannotMarkExternalFileReplacedDuringConfirmation(t *testing.T) {
	u := validDownloadFixture(t, func(w http.ResponseWriter, r *http.Request) { w.Write(dmgFixture()) })
	u.Check()
	if state := u.Download(); state.Status != "ready" {
		t.Fatal(state)
	}
	external := filepath.Join(t.TempDir(), "다른 사용자 자료.dmg")
	if err := os.WriteFile(external, dmgFixture(), 0600); err != nil {
		t.Fatal(err)
	}
	u.verifySignature = func(context.Context, string, bool) (bool, error) { return false, nil }
	u.confirmInstall = func(UpdateState) (bool, error) {
		if err := os.Remove(u.installerPath); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(external, u.installerPath); err != nil {
			t.Fatal(err)
		}
		return true, nil
	}
	u.launchInstaller = func(string, string) error { t.Fatal("must not launch symlink"); return nil }
	if got := u.Install(); got.Status != "error" {
		t.Fatal(got)
	}
	if _, err := unix.Getxattr(external, "com.apple.quarantine", make([]byte, 1024)); err == nil {
		t.Fatal("installer provenance was written to an external file")
	}
}

func fakeBundle(t *testing.T, dir, version, id string) string {
	t.Helper()
	bundle := filepath.Join(dir, appName+".app")
	for _, folder := range []string{"Contents/MacOS", "Contents/Resources"} {
		if err := os.MkdirAll(filepath.Join(bundle, folder), 0755); err != nil {
			t.Fatal(err)
		}
	}
	plist := `<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleExecutable</key><string>` + appName + `</string><key>CFBundleIdentifier</key><string>` + id + `</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>`
	files := map[string]string{
		"Contents/Info.plist":             plist,
		"Contents/MacOS/" + appName:       "#!/bin/sh\nexit 0\n",
		"Contents/Resources/release.json": `{"name":"` + appName + `","version":"` + version + `","bundle_id":"` + id + `"}`,
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(bundle, name), []byte(body), 0755); err != nil {
			t.Fatal(err)
		}
	}
	if output, err := exec.Command("/usr/bin/codesign", "--force", "--deep", "-s", "-", bundle).CombinedOutput(); err != nil {
		t.Fatal(string(output))
	}
	return bundle
}

func fakeImage(t *testing.T, version, id string) string {
	t.Helper()
	source := t.TempDir()
	fakeBundle(t, source, version, id)
	image := filepath.Join(t.TempDir(), appName+"-"+version+"-macos-arm64.dmg")
	if output, err := exec.Command("/usr/bin/hdiutil", "create", "-quiet", "-fs", "HFS+", "-srcfolder", source, "-volname", appName, image).CombinedOutput(); err != nil {
		t.Fatal(string(output))
	}
	if err := markInstallerOrigin(image, ""); err != nil {
		t.Fatal(err)
	}
	return image
}

func finishedPID(t *testing.T) int {
	t.Helper()
	command := exec.Command("/usr/bin/true")
	if err := command.Run(); err != nil {
		t.Fatal(err)
	}
	return command.Process.Pid
}

func installedVersion(t *testing.T, bundle string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(bundle, "Contents", "Resources", "release.json"))
	if err != nil {
		t.Fatal(err)
	}
	var release struct{ Version string }
	if err := json.Unmarshal(data, &release); err != nil {
		t.Fatal(err)
	}
	return release.Version
}

func leftovers(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, entry := range entries {
		if entry.Name() != appName+".app" {
			names = append(names, entry.Name())
		}
	}
	return names
}

func TestMacUpdateReplacesBundleFromImageAndRestartsWithoutQuarantine(t *testing.T) {
	apps := t.TempDir()
	bundle := fakeBundle(t, apps, "1.2.0-rc.7", bundleID)
	image := fakeImage(t, "1.2.0-rc.8", bundleID)
	var opened []string
	err := replaceBundle(bundle, image, "1.2.0-rc.8", finishedPID(t), func(path string) error { opened = append(opened, path); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if got := installedVersion(t, bundle); got != "1.2.0-rc.8" || len(opened) != 1 || opened[0] != bundle {
		t.Fatal(got, opened)
	}
	if names := leftovers(t, apps); len(names) != 0 {
		t.Fatal(names)
	}
	if _, err := unix.Getxattr(filepath.Join(bundle, "Contents", "MacOS", appName), "com.apple.quarantine", make([]byte, 256)); err == nil {
		t.Fatal("replaced app keeps quarantine")
	}
}

func TestMacUpdateKeepsCurrentAppWhenImageDoesNotMatchTheRelease(t *testing.T) {
	for name, image := range map[string]func(*testing.T) string{
		"version":    func(t *testing.T) string { return fakeImage(t, "1.2.0-rc.9", bundleID) },
		"identifier": func(t *testing.T) string { return fakeImage(t, "1.2.0-rc.8", "app.other.desktop") },
	} {
		t.Run(name, func(t *testing.T) {
			apps := t.TempDir()
			bundle := fakeBundle(t, apps, "1.2.0-rc.7", bundleID)
			err := replaceBundle(bundle, image(t), "1.2.0-rc.8", finishedPID(t), func(string) error { t.Fatal("must not open"); return nil })
			if err == nil || installedVersion(t, bundle) != "1.2.0-rc.7" {
				t.Fatal(err)
			}
			if names := leftovers(t, apps); len(names) != 0 {
				t.Fatal(names)
			}
		})
	}
}

func TestMacUpdateRestoresCurrentAppWhenRestartFails(t *testing.T) {
	apps := t.TempDir()
	bundle := fakeBundle(t, apps, "1.2.0-rc.7", bundleID)
	err := replaceBundle(bundle, fakeImage(t, "1.2.0-rc.8", bundleID), "1.2.0-rc.8", finishedPID(t), func(string) error { return errors.New("launch failed") })
	if err == nil || installedVersion(t, bundle) != "1.2.0-rc.7" {
		t.Fatal(err)
	}
	if names := leftovers(t, apps); len(names) != 0 {
		t.Fatal(names)
	}
}

func TestMacUpdateRejectsTamperedBundleInImage(t *testing.T) {
	source := t.TempDir()
	inner := fakeBundle(t, source, "1.2.0-rc.8", bundleID)
	if err := os.WriteFile(filepath.Join(inner, "Contents", "MacOS", appName), []byte("#!/bin/sh\nexit 1\n"), 0755); err != nil {
		t.Fatal(err)
	}
	image := filepath.Join(t.TempDir(), appName+"-1.2.0-rc.8-macos-arm64.dmg")
	if output, err := exec.Command("/usr/bin/hdiutil", "create", "-quiet", "-fs", "HFS+", "-srcfolder", source, image).CombinedOutput(); err != nil {
		t.Fatal(string(output))
	}
	apps := t.TempDir()
	bundle := fakeBundle(t, apps, "1.2.0-rc.7", bundleID)
	if err := replaceBundle(bundle, image, "1.2.0-rc.8", finishedPID(t), func(string) error { t.Fatal("must not open"); return nil }); err == nil || installedVersion(t, bundle) != "1.2.0-rc.7" {
		t.Fatal(err)
	}
}
