package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
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
func launchUpdateInstaller(path, version string) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	if _, err := replaceableBundle(executable); err != nil {
		return err
	}
	command := exec.Command(executable, applyUpdateFlag, path, version, strconv.Itoa(os.Getpid()))
	command.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := command.Start(); err != nil {
		return err
	}
	return command.Process.Release()
}

func replaceableBundle(executable string) (string, error) {
	resolved, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return "", err
	}
	bundle := filepath.Dir(filepath.Dir(filepath.Dir(resolved)))
	if filepath.Base(bundle) != appName+".app" || filepath.Base(filepath.Dir(resolved)) != "MacOS" || strings.Contains(bundle, "/AppTranslocation/") {
		return "", errUpdateInvalid
	}
	if err := unix.Access(filepath.Dir(bundle), unix.W_OK); err != nil {
		return "", err
	}
	return bundle, nil
}

func applyStagedUpdate(image, version string, pid int) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	bundle, err := replaceableBundle(executable)
	if err != nil {
		return err
	}
	if !filepath.IsAbs(image) || !strings.HasPrefix(filepath.Base(image), appName+"-"+version+"-macos-") || filepath.Ext(image) != ".dmg" {
		return errUpdateInvalid
	}
	err = replaceBundle(bundle, image, version, pid, openApp)
	if err != nil {
		recordUpdateFailure(err)
		_ = openApp(bundle)
		return err
	}
	if folder := filepath.Dir(image); strings.HasPrefix(filepath.Base(folder), appName+"-update-") {
		_ = os.RemoveAll(folder)
	}
	return nil
}

func openApp(bundle string) error {
	return exec.Command("/usr/bin/open", bundle).Run()
}

func recordUpdateFailure(failure error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return
	}
	folder := filepath.Join(home, "Library", "Logs", appName)
	if os.MkdirAll(folder, 0700) != nil {
		return
	}
	line := time.Now().Format(time.RFC3339) + " " + failure.Error() + "\n"
	f, err := os.OpenFile(filepath.Join(folder, "update.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.WriteString(line)
}

func waitForExit(pid int, limit time.Duration) error {
	deadline := time.Now().Add(limit)
	for unix.Kill(pid, 0) == nil {
		if time.Now().After(deadline) {
			return errors.New("이전 앱이 종료되지 않았습니다")
		}
		time.Sleep(250 * time.Millisecond)
	}
	return nil
}

func replaceBundle(bundle, image, version string, pid int, open func(string) error) error {
	if err := waitForExit(pid, time.Minute); err != nil {
		return err
	}
	work, err := os.MkdirTemp("", "huginn-update-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)
	mount := filepath.Join(work, "image")
	attach := exec.Command("/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount, image)
	if output, err := attach.CombinedOutput(); err != nil {
		return fmt.Errorf("DMG를 열지 못했습니다: %v %s", err, strings.TrimSpace(string(output)))
	}
	defer exec.Command("/usr/bin/hdiutil", "detach", "-force", mount).Run()
	source := filepath.Join(mount, appName+".app")
	if err := verifyBundle(source, version); err != nil {
		return err
	}
	parent := filepath.Dir(bundle)
	suffix := strconv.FormatInt(time.Now().UnixNano(), 36)
	staged := filepath.Join(parent, "."+appName+"-update-"+suffix+".app")
	previous := filepath.Join(parent, "."+appName+"-previous-"+suffix+".app")
	if output, err := exec.Command("/usr/bin/ditto", source, staged).CombinedOutput(); err != nil {
		_ = os.RemoveAll(staged)
		return fmt.Errorf("새 앱을 복사하지 못했습니다: %v %s", err, strings.TrimSpace(string(output)))
	}
	if err := clearQuarantine(staged); err != nil {
		_ = os.RemoveAll(staged)
		return err
	}
	if err := verifyBundle(staged, version); err != nil {
		_ = os.RemoveAll(staged)
		return err
	}
	if err := os.Rename(bundle, previous); err != nil {
		_ = os.RemoveAll(staged)
		return err
	}
	if err := os.Rename(staged, bundle); err != nil {
		_ = os.Rename(previous, bundle)
		_ = os.RemoveAll(staged)
		return err
	}
	if err := open(bundle); err != nil {
		_ = os.Rename(bundle, staged)
		_ = os.Rename(previous, bundle)
		_ = os.RemoveAll(staged)
		return err
	}
	_ = os.RemoveAll(previous)
	return nil
}

func verifyBundle(bundle, version string) error {
	data, err := os.ReadFile(filepath.Join(bundle, "Contents", "Resources", "release.json"))
	if err != nil {
		return err
	}
	var release struct {
		Name     string `json:"name"`
		Version  string `json:"version"`
		BundleID string `json:"bundle_id"`
	}
	if json.Unmarshal(data, &release) != nil || release.Name != appName || release.Version != version || release.BundleID != bundleID {
		return errors.New("새 앱의 배포 정보가 업데이트와 일치하지 않습니다")
	}
	info, err := exec.Command("/usr/bin/plutil", "-extract", "CFBundleIdentifier", "raw", filepath.Join(bundle, "Contents", "Info.plist")).Output()
	if err != nil || strings.TrimSpace(string(info)) != bundleID {
		return errors.New("새 앱의 식별자가 일치하지 않습니다")
	}
	if output, err := exec.Command("/usr/bin/codesign", "--verify", "--deep", "--strict", bundle).CombinedOutput(); err != nil {
		return fmt.Errorf("새 앱의 코드 무결성을 확인하지 못했습니다: %s", strings.TrimSpace(string(output)))
	}
	return nil
}

func clearQuarantine(root string) error {
	return filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := unix.Lremovexattr(path, "com.apple.quarantine"); err != nil && !errors.Is(err, unix.ENOATTR) {
			return err
		}
		return nil
	})
}
