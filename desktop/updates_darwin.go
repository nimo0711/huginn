package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

func markInstallerOrigin(path, _ string) error {
	value := []byte(fmt.Sprintf("0081;%x;Huginn;", time.Now().Unix()))
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
	command := exec.CommandContext(ctx, "/usr/bin/codesign", "--verify", "--strict", path)
	command.Env = append(os.Environ(), "LC_ALL=C", "LANG=C")
	var output limitedUpdateOutput
	command.Stdout = &output
	command.Stderr = &output
	err := command.Run()
	if err != nil {
		// Only codesign's explicit unsigned result is allowed for prereleases.
		// Invalid signatures, failed commands and cancellation must remain errors.
		var exitError *exec.ExitError
		if !required && ctx.Err() == nil && errors.As(err, &exitError) && exitError.ExitCode() == 1 && strings.TrimSpace(output.String()) == path+": code object is not signed at all" {
			return false, nil
		}
		return false, errUpdateInvalid
	}
	// A signed image must also satisfy Gatekeeper's disk-image policy. Keeping
	// quarantine and using normal open preserves OS checks for unsigned images.
	if err := exec.CommandContext(ctx, "/usr/sbin/spctl", "--assess", "--type", "open", "--context", "context:primary-signature", path).Run(); err != nil {
		return false, errUpdateInvalid
	}
	return true, nil
}
func launchUpdateInstaller(path string) error {
	return exec.Command("/usr/bin/open", path).Run()
}
