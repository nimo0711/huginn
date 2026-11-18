package main

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

func markInstallerOrigin(path, _ string) error {
	value := []byte(fmt.Sprintf("0081;%x;Forkrail;", time.Now().Unix()))
	if err := unix.Setxattr(path, "com.apple.quarantine", value, 0); err != nil {
		return err
	}
	actual := make([]byte, len(value)+1)
	n, err := unix.Getxattr(path, "com.apple.quarantine", actual)
	if err != nil || string(actual[:n]) != string(value) {
		return errUpdateInvalid
	}
	return nil
}

func verifyInstallerSignature(ctx context.Context, path string, required bool) (bool, error) {
	command := exec.CommandContext(ctx, "/usr/sbin/pkgutil", "--check-signature", path)
	output, err := command.Output()
	if err != nil {
		// Unsigned prereleases are supported only through normal Installer/Gatekeeper
		// UI. Other signature errors must not be reinterpreted as unsigned packages.
		if !required && strings.Contains(string(output), "Status: no signature") {
			return false, nil
		}
		return false, errUpdateInvalid
	}
	if !strings.Contains(string(output), "Status: signed by a certificate trusted by") {
		return false, errUpdateInvalid
	}
	if err := exec.CommandContext(ctx, "/usr/sbin/spctl", "--assess", "--type", "install", path).Run(); err != nil {
		return false, errUpdateInvalid
	}
	return true, nil
}
func launchUpdateInstaller(path string) error {
	return exec.Command("/usr/bin/open", "-a", "Installer", path).Run()
}
