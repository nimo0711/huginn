package main

import (
	"context"
	"golang.org/x/sys/windows"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func markInstallerOrigin(path, source string) error {
	// Preserve normal Shell/SmartScreen handling of Internet downloads. The
	// source is the fixed public release page, never a signed asset URL or token.
	value := "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=" + source + "\r\n"
	if err := os.WriteFile(path+":Zone.Identifier", []byte(value), 0600); err != nil {
		return err
	}
	actual, err := os.ReadFile(path + ":Zone.Identifier")
	if err != nil || string(actual) != value {
		return errUpdateInvalid
	}
	return nil
}

func verifyInstallerSignature(ctx context.Context, path string, required bool) (bool, error) {
	// The path is environment data, never PowerShell source or a command argument
	// interpolated into source. The output is a small status enum, never shown.
	script := `$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:FORKRAIL_UPDATE_INSTALLER; [Console]::Write($s.Status.ToString())`
	command := exec.CommandContext(ctx, filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", script)
	configureChild(command)
	command.Env = append(os.Environ(), "FORKRAIL_UPDATE_INSTALLER="+path)
	output, err := command.Output()
	if err != nil {
		return false, errUpdateInvalid
	}
	switch strings.TrimSpace(string(output)) {
	case "Valid":
		return true, nil
	case "NotSigned":
		if !required {
			return false, nil
		}
	}
	return false, errUpdateInvalid
}
func launchUpdateInstaller(path string) error {
	verb, err := windows.UTF16PtrFromString("open")
	if err != nil {
		return err
	}
	file, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	// ShellExecute invokes the installer's normal UAC/SmartScreen path. No silent
	// parameters, elevation bypass, temporary script or process tree termination.
	return windows.ShellExecute(0, verb, file, nil, nil, windows.SW_SHOWNORMAL)
}
