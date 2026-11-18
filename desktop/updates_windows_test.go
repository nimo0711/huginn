package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWindowsUpdatePreservesInternetZoneOnDownloadedInstaller(t *testing.T) {
	path := filepath.Join(t.TempDir(), "한글 설치 파일.exe")
	if err := os.WriteFile(path, []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	source := "https://github.com/owner/forkrail/releases/tag/v1.2.0-rc.2"
	if err := markInstallerOrigin(path, source); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path + ":Zone.Identifier")
	if err != nil || !strings.Contains(string(data), "ZoneId=3\r\n") || !strings.Contains(string(data), "HostUrl="+source+"\r\n") {
		t.Fatalf("missing Internet zone: %q %v", data, err)
	}
}
