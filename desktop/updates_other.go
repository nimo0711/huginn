//go:build !darwin && !windows

package main

import "context"

func verifyInstallerSignature(context.Context, string, bool) (bool, error) {
	return false, errUpdateInvalid
}
func launchUpdateInstaller(string) error { return errUpdateInvalid }

func markInstallerOrigin(string, string) error { return errUpdateInvalid }
