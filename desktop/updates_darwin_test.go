package main

import (
	"context"
	"fmt"
	"golang.org/x/sys/unix"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMacUpdatePreservesQuarantineOnDownloadedInstaller(t *testing.T) {
	path := filepath.Join(t.TempDir(), "한글 설치 파일.pkg")
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
	u := validDownloadFixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "xar!test") })
	u.Check()
	if state := u.Download(); state.Status != "ready" {
		t.Fatal(state)
	}
	external := filepath.Join(t.TempDir(), "다른 사용자 자료.pkg")
	if err := os.WriteFile(external, []byte("xar!test"), 0600); err != nil {
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
