package main

import (
	"context"
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
	if err := markInstallerOrigin(path, "https://github.com/owner/forkrail/releases/tag/v1.2.0-rc.2"); err != nil {
		t.Fatal(err)
	}
	data := make([]byte, 1024)
	n, err := unix.Getxattr(path, "com.apple.quarantine", data)
	if err != nil || !strings.HasPrefix(string(data[:n]), "0081;") || !strings.Contains(string(data[:n]), ";Forkrail;") {
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
	u.launchInstaller = func(string) error { t.Fatal("must not launch symlink"); return nil }
	if got := u.Install(); got.Status != "error" {
		t.Fatal(got)
	}
	if _, err := unix.Getxattr(external, "com.apple.quarantine", make([]byte, 1024)); err == nil {
		t.Fatal("installer provenance was written to an external file")
	}
}
