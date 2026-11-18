//go:build !windows

package main

import (
	"os"
	"os/exec"
	"syscall"
)

func configureChild(_ *exec.Cmd) {}

func signalChild(process *os.Process) error {
	return process.Signal(syscall.SIGTERM)
}
