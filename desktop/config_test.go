package main

import (
	"path/filepath"
	"testing"
)

func TestExplicitStateFilesRequireOwnedService(t *testing.T) {
	for _, name := range []string{"HUGINN_DATA_DIR", "WORK_BOARD_DB", "WORK_BOARD_SPOOL"} {
		t.Run(name, func(t *testing.T) {
			t.Setenv("HUGINN_DATA_DIR", "")
			t.Setenv("WORK_BOARD_DB", "")
			t.Setenv("WORK_BOARD_SPOOL", "")
			t.Setenv(name, filepath.Join(t.TempDir(), "보드 데이터"))
			cfg, err := desktopConfig("")
			if err != nil || !cfg.requireOwned {
				t.Fatalf("explicit %s must prevent reusing an existing service: %v", name, err)
			}
		})
	}
}
