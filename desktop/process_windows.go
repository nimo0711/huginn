package main

import (
	"os"
	"os/exec"
	"syscall"
)

func configureChild(cmd *exec.Cmd) {
	const createNoWindow = 0x08000000
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
}

func signalChild(process *os.Process) error {
	return process.Kill()
}
