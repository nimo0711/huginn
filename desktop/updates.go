package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

const maxUpdateInstaller int64 = 1 << 30
const maxUpdateMetadata int64 = 64 << 10

var errUpdateAuth = errors.New("GitHub 저장소 접근 권한이 필요합니다. GitHub CLI 로그인과 저장소 권한을 확인해 주세요.")
var errUpdateInvalid = errors.New("배포 파일의 버전·대상·크기 또는 체크섬이 올바르지 않습니다.")
var updateVersionPattern = regexp.MustCompile(`^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})(?:-(alpha|beta|rc)\.(0|[1-9][0-9]{0,8}))?$`)
var updateRepositoryPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$`)
var updateSHA = regexp.MustCompile(`^[0-9a-f]{64}$`)
var updateCommit = regexp.MustCompile(`^[0-9a-f]{40}$`)

type updateVersion struct{ major, minor, patch, channel, number int }

func parseUpdateVersion(value string) (updateVersion, error) {
	m := updateVersionPattern.FindStringSubmatch(value)
	if m == nil {
		return updateVersion{}, errUpdateInvalid
	}
	v := updateVersion{channel: 3}
	v.major, _ = strconv.Atoi(m[1])
	v.minor, _ = strconv.Atoi(m[2])
	v.patch, _ = strconv.Atoi(m[3])
	if m[4] != "" {
		v.channel = map[string]int{"alpha": 0, "beta": 1, "rc": 2}[m[4]]
		v.number, _ = strconv.Atoi(m[5])
	}
	return v, nil
}
func (v updateVersion) compare(other updateVersion) int {
	a := []int{v.major, v.minor, v.patch, v.channel, v.number}
	b := []int{other.major, other.minor, other.patch, other.channel, other.number}
	for i := range a {
		if a[i] < b[i] {
			return -1
		}
		if a[i] > b[i] {
			return 1
		}
	}
	return 0
}

type UpdateState struct {
	Status          string `json:"status"`
	CurrentVersion  string `json:"current_version"`
	LatestVersion   string `json:"latest_version"`
	Target          string `json:"target"`
	OS              string `json:"os"`
	ReleaseURL      string `json:"release_url"`
	InstallerName   string `json:"installer_name"`
	DownloadedBytes int64  `json:"downloaded_bytes"`
	TotalBytes      int64  `json:"total_bytes"`
	Signed          bool   `json:"signed"`
	Message         string `json:"message"`
	Available       bool   `json:"available"`
}
type updateConfig struct{ version, repository, target, tempRoot string }
type updateAsset struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
	Size int64  `json:"size"`
}
type updateRelease struct {
	Tag        string        `json:"tag_name"`
	Draft      bool          `json:"draft"`
	Prerelease bool          `json:"prerelease"`
	Assets     []updateAsset `json:"assets"`
}
type updateManifest struct {
	Version      string   `json:"version"`
	Target       string   `json:"target"`
	Signed       *bool    `json:"signed"`
	SourceCommit string   `json:"source_commit"`
	SourceDirty  *bool    `json:"source_dirty"`
	Artifacts    []string `json:"artifacts"`
}

// Only these four exported methods are bound to the webview. Paths, URLs, tokens,
// commands and release selection are never accepted from the page.
type AppUpdates struct {
	config                                     updateConfig
	ctx                                        context.Context
	cancel                                     context.CancelFunc
	mu                                         sync.Mutex
	operation                                  sync.Mutex
	state                                      UpdateState
	selected                                   *updateRelease
	client                                     *http.Client
	installerPath, temporaryDir, installerHash string
	installerSize                              int64
	launched                                   bool
	authToken                                  func(context.Context) string
	verifySignature                            func(context.Context, string, bool) (bool, error)
	markOrigin                                 func(string, string) error
	confirmInstall                             func(UpdateState) (bool, error)
	launchInstaller                            func(string) error
	afterLaunch                                func()
}

func nativeUpdateConfig(executable, version string) (updateConfig, error) {
	cfg := updateConfig{version: version}
	switch runtime.GOOS + "/" + runtime.GOARCH {
	case "darwin/arm64":
		cfg.target = "macos-arm64"
	case "darwin/amd64":
		cfg.target = "macos-x86_64"
	case "windows/amd64":
		cfg.target = "windows-x64"
	default:
		return cfg, errUpdateInvalid
	}
	path := filepath.Join(filepath.Dir(executable), "release.json")
	if runtime.GOOS == "darwin" {
		path = filepath.Join(filepath.Dir(executable), "..", "Resources", "release.json")
	}
	f, err := os.Open(path)
	if err != nil {
		return cfg, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxUpdateMetadata+1))
	if err != nil || int64(len(data)) > maxUpdateMetadata {
		return cfg, errUpdateInvalid
	}
	var metadata struct {
		Name, Version, Repository string
		BundleID                  string `json:"bundle_id"`
		Platforms                 []string
	}
	if json.Unmarshal(data, &metadata) != nil || metadata.Name != productID || metadata.Version != version || metadata.BundleID != bundleID || !updateRepositoryPattern.MatchString(metadata.Repository) {
		return cfg, errUpdateInvalid
	}
	found := false
	for _, target := range metadata.Platforms {
		if target == cfg.target {
			found = true
		}
	}
	if !found {
		return cfg, errUpdateInvalid
	}
	cfg.repository = metadata.Repository
	return cfg, nil
}
func newAppUpdates(ctx context.Context, cfg updateConfig, client *http.Client) *AppUpdates {
	ctx, cancel := context.WithCancel(ctx)
	if client == nil {
		client = &http.Client{Transport: &http.Transport{Proxy: http.ProxyFromEnvironment, ResponseHeaderTimeout: 30 * time.Second, IdleConnTimeout: 30 * time.Second}, Timeout: 15 * time.Minute}
	}
	u := &AppUpdates{config: cfg, ctx: ctx, cancel: cancel, client: client, authToken: githubCLIToken, verifySignature: verifyInstallerSignature, markOrigin: markInstallerOrigin, launchInstaller: launchUpdateInstaller}
	osName := "other"
	if strings.HasPrefix(cfg.target, "macos-") {
		osName = "macos"
	}
	if cfg.target == "windows-x64" {
		osName = "windows"
	}
	_, err := parseUpdateVersion(cfg.version)
	valid := err == nil && updateRepositoryPattern.MatchString(cfg.repository) && (cfg.target == "macos-arm64" || cfg.target == "macos-x86_64" || cfg.target == "windows-x64")
	u.state = UpdateState{Status: "idle", CurrentVersion: cfg.version, Target: cfg.target, OS: osName, Available: valid}
	if updateRepositoryPattern.MatchString(cfg.repository) {
		u.state.ReleaseURL = "https://github.com/" + cfg.repository + "/releases"
	}
	if !valid {
		u.state.Status = "unsupported"
		u.state.Message = "이 실행본의 배포 정보를 확인할 수 없습니다. 최신 설치본을 사용해 주세요."
	}
	return u
}
func (u *AppUpdates) Info() UpdateState { u.mu.Lock(); defer u.mu.Unlock(); return u.state }
func (u *AppUpdates) change(fn func(*UpdateState)) UpdateState {
	u.mu.Lock()
	defer u.mu.Unlock()
	fn(&u.state)
	return u.state
}
func (u *AppUpdates) fail(err error) UpdateState {
	status := "error"
	message := "업데이트를 처리하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요."
	if errors.Is(err, errUpdateAuth) {
		status = "auth_required"
		message = errUpdateAuth.Error()
	} else if errors.Is(err, errUpdateInvalid) {
		message = errUpdateInvalid.Error()
	} else if errors.Is(err, context.Canceled) {
		message = "업데이트 작업이 취소되었습니다."
	}
	return u.change(func(s *UpdateState) { s.Status = status; s.Message = message })
}
func (u *AppUpdates) begin() bool {
	if !u.operation.TryLock() {
		return false
	}
	if !u.Info().Available || u.ctx.Err() != nil || u.launched {
		u.operation.Unlock()
		return false
	}
	return true
}
func (u *AppUpdates) clearDownload() {
	if u.temporaryDir != "" && !u.launched {
		_ = os.RemoveAll(u.temporaryDir)
	}
	u.temporaryDir = ""
	u.installerPath = ""
	u.installerHash = ""
	u.installerSize = 0
}
func (u *AppUpdates) shutdown() {
	u.cancel()
	u.operation.Lock()
	defer u.operation.Unlock()
	u.clearDownload()
	u.client.CloseIdleConnections()
}

func (u *AppUpdates) Check() UpdateState {
	if !u.begin() {
		return u.Info()
	}
	defer u.operation.Unlock()
	u.clearDownload()
	u.selected = nil
	u.change(func(s *UpdateState) {
		s.Status = "checking"
		s.Message = "게시된 릴리즈를 확인하고 있습니다."
		s.LatestVersion = ""
		s.InstallerName = ""
		s.DownloadedBytes = 0
		s.TotalBytes = 0
		s.Signed = false
		s.ReleaseURL = "https://github.com/" + u.config.repository + "/releases"
	})
	ctx, cancel := context.WithTimeout(u.ctx, 60*time.Second)
	defer cancel()
	token := u.authToken(ctx)
	base := "https://api.github.com/repos/" + u.config.repository
	// A repository 404 is deliberately not called 'no releases': GitHub hides
	// inaccessible private repositories behind 404, including expired auth.
	if _, err := u.fetchSmall(ctx, base, token, false, 1<<20); err != nil {
		return u.fail(err)
	}
	current, _ := parseUpdateVersion(u.config.version)
	var best *updateRelease
	var bestVersion updateVersion
	for page := 1; page <= 10; page++ {
		data, err := u.fetchSmall(ctx, base+"/releases?per_page=100&page="+strconv.Itoa(page), token, false, 8<<20)
		if err != nil {
			return u.fail(err)
		}
		var releases []updateRelease
		if json.Unmarshal(data, &releases) != nil || releases == nil || len(releases) > 100 {
			return u.fail(errUpdateInvalid)
		}
		for _, release := range releases {
			if release.Draft || !strings.HasPrefix(release.Tag, "v") {
				continue
			}
			version, err := parseUpdateVersion(strings.TrimPrefix(release.Tag, "v"))
			if err != nil || release.Prerelease != (version.channel != 3) || (current.channel == 3 && version.channel != 3) {
				continue
			}
			if best == nil || version.compare(bestVersion) > 0 {
				copy := release
				best = &copy
				bestVersion = version
			}
		}
		if len(releases) < 100 {
			break
		}
		if page == 10 {
			return u.fail(errUpdateInvalid)
		}
	}
	if best == nil {
		return u.change(func(s *UpdateState) {
			s.Status = "unpublished"
			s.Message = "이 업데이트 채널에 게시된 릴리즈가 없습니다."
		})
	}
	latest := strings.TrimPrefix(best.Tag, "v")
	u.change(func(s *UpdateState) {
		s.LatestVersion = latest
		s.ReleaseURL = "https://github.com/" + u.config.repository + "/releases/tag/" + best.Tag
	})
	if bestVersion.compare(current) <= 0 {
		return u.change(func(s *UpdateState) { s.Status = "current"; s.Message = "현재 버전이 최신입니다." })
	}
	installer, _, _, err := u.releaseAssets(best)
	if err != nil {
		return u.fail(err)
	}
	u.selected = best
	return u.change(func(s *UpdateState) {
		s.Status = "available"
		s.InstallerName = installer.Name
		s.TotalBytes = installer.Size
		s.Message = "새 버전을 다운로드할 수 있습니다."
	})
}
func installerName(version, target string) string {
	suffix := ".dmg"
	if target == "windows-x64" {
		suffix = "-Setup.exe"
	}
	return "Forkrail-" + version + "-" + target + suffix
}
func (u *AppUpdates) releaseAssets(release *updateRelease) (updateAsset, updateAsset, updateAsset, error) {
	version := strings.TrimPrefix(release.Tag, "v")
	if _, err := parseUpdateVersion(version); err != nil {
		return updateAsset{}, updateAsset{}, updateAsset{}, err
	}
	wanted := []string{installerName(version, u.config.target), u.config.target + "-release-manifest.json", u.config.target + "-SHA256SUMS"}
	var found [3]updateAsset
	seen := map[string]bool{}
	ids := map[int64]bool{}
	if len(release.Assets) > 100 {
		return found[0], found[1], found[2], errUpdateInvalid
	}
	for _, a := range release.Assets {
		if seen[a.Name] || ids[a.ID] || a.ID <= 0 || a.Size <= 0 || a.Size > maxUpdateInstaller || len(a.Name) > 200 || strings.ContainsAny(a.Name, "/\\\r\n\x00") {
			return found[0], found[1], found[2], errUpdateInvalid
		}
		seen[a.Name] = true
		ids[a.ID] = true
		for i, name := range wanted {
			if a.Name == name {
				found[i] = a
				if i > 0 && a.Size > maxUpdateMetadata {
					return found[0], found[1], found[2], errUpdateInvalid
				}
			}
		}
	}
	for _, a := range found {
		if a.ID == 0 {
			return found[0], found[1], found[2], errUpdateInvalid
		}
	}
	return found[0], found[1], found[2], nil
}
func (u *AppUpdates) assetURL(a updateAsset) string {
	return "https://api.github.com/repos/" + u.config.repository + "/releases/assets/" + strconv.FormatInt(a.ID, 10)
}
func (u *AppUpdates) Download() UpdateState {
	if !u.begin() {
		return u.Info()
	}
	defer u.operation.Unlock()
	if u.selected == nil {
		return u.Info()
	}
	if u.Info().Status == "ready" {
		return u.Info()
	}
	u.clearDownload()
	u.change(func(s *UpdateState) {
		s.Status = "downloading"
		s.DownloadedBytes = 0
		s.Message = "설치 파일을 다운로드하고 검증하고 있습니다."
	})
	ctx, cancel := context.WithTimeout(u.ctx, 15*time.Minute)
	defer cancel()
	token := u.authToken(ctx)
	installer, manifestAsset, sumAsset, err := u.releaseAssets(u.selected)
	if err != nil {
		return u.fail(err)
	}
	sums, err := u.fetchSmall(ctx, u.assetURL(sumAsset), token, true, maxUpdateMetadata)
	if err != nil {
		return u.fail(err)
	}
	if int64(len(sums)) != sumAsset.Size {
		return u.fail(errUpdateInvalid)
	}
	checksums, err := parseUpdateChecksums(sums)
	if err != nil {
		return u.fail(err)
	}
	data, err := u.fetchSmall(ctx, u.assetURL(manifestAsset), token, true, maxUpdateMetadata)
	if err != nil {
		return u.fail(err)
	}
	if int64(len(data)) != manifestAsset.Size || checksums[manifestAsset.Name] != fmt.Sprintf("%x", sha256.Sum256(data)) {
		return u.fail(errUpdateInvalid)
	}
	version := strings.TrimPrefix(u.selected.Tag, "v")
	var manifest updateManifest
	if json.Unmarshal(data, &manifest) != nil || manifest.Version != version || manifest.Target != u.config.target || manifest.Signed == nil || manifest.SourceDirty == nil || *manifest.SourceDirty || !updateCommit.MatchString(manifest.SourceCommit) || len(manifest.Artifacts) != 2 {
		return u.fail(errUpdateInvalid)
	}
	expectedZip := "Forkrail-" + version + "-" + u.config.target + ".zip"
	if !((manifest.Artifacts[0] == installer.Name && manifest.Artifacts[1] == expectedZip) || (manifest.Artifacts[1] == installer.Name && manifest.Artifacts[0] == expectedZip)) {
		return u.fail(errUpdateInvalid)
	}
	if !strings.Contains(version, "-") && !*manifest.Signed {
		return u.fail(errUpdateInvalid)
	}
	expectedHash := checksums[installer.Name]
	if !updateSHA.MatchString(expectedHash) {
		return u.fail(errUpdateInvalid)
	}
	u.change(func(s *UpdateState) { s.Signed = *manifest.Signed; s.TotalBytes = installer.Size })
	directory, err := os.MkdirTemp(u.config.tempRoot, "Forkrail-update-")
	if err != nil {
		return u.fail(err)
	}
	u.temporaryDir = directory
	success := false
	defer func() {
		if !success {
			u.clearDownload()
		}
	}()
	partial := filepath.Join(directory, "installer.partial")
	f, err := os.OpenFile(partial, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return u.fail(err)
	}
	response, err := u.request(ctx, u.assetURL(installer), token, true)
	if err != nil {
		f.Close()
		return u.fail(err)
	}
	if response.ContentLength > 0 && response.ContentLength != installer.Size {
		response.Body.Close()
		f.Close()
		return u.fail(errUpdateInvalid)
	}
	hash := sha256.New()
	writer := &updateProgress{writer: io.MultiWriter(f, hash), updater: u}
	count, copyErr := io.Copy(writer, io.LimitReader(response.Body, installer.Size+1))
	response.Body.Close()
	syncErr := f.Sync()
	closeErr := f.Close()
	if copyErr != nil {
		return u.fail(copyErr)
	}
	if syncErr != nil || closeErr != nil {
		return u.fail(errors.New("file write failed"))
	}
	if count != installer.Size || hex.EncodeToString(hash.Sum(nil)) != expectedHash {
		return u.fail(errUpdateInvalid)
	}
	path := filepath.Join(directory, installer.Name)
	if err := os.Rename(partial, path); err != nil {
		return u.fail(err)
	}
	u.installerPath = path
	u.installerHash = expectedHash
	u.installerSize = installer.Size
	if err := u.markOrigin(path, u.Info().ReleaseURL); err != nil {
		return u.fail(errUpdateInvalid)
	}
	if err := u.validateInstaller(); err != nil {
		return u.fail(err)
	}
	success = true
	return u.change(func(s *UpdateState) {
		s.Status = "ready"
		s.Message = "파일 검증을 마쳤습니다. 설치 버튼을 누르면 OS 설치창을 엽니다."
		if s.OS == "macos" {
			s.Message = "파일 검증을 마쳤습니다. DMG를 열고 앱을 Applications로 끌어다 놓으면 업데이트할 수 있습니다."
		}
	})
}

type updateProgress struct {
	writer  io.Writer
	updater *AppUpdates
}

func (p *updateProgress) Write(data []byte) (int, error) {
	n, err := p.writer.Write(data)
	p.updater.change(func(s *UpdateState) { s.DownloadedBytes += int64(n) })
	return n, err
}
func parseUpdateChecksums(data []byte) (map[string]string, error) {
	result := map[string]string{}
	rows := strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
	if len(rows) == 0 || len(rows) > 10 {
		return nil, errUpdateInvalid
	}
	for _, row := range rows {
		if len(row) < 67 || row[64:66] != "  " || !updateSHA.MatchString(row[:64]) {
			return nil, errUpdateInvalid
		}
		name := row[66:]
		if len(name) > 200 || name == "." || name == ".." || strings.ContainsAny(name, "/\\\r\n\x00") || result[name] != "" {
			return nil, errUpdateInvalid
		}
		result[name] = row[:64]
	}
	return result, nil
}
func (u *AppUpdates) validateInstaller() error {
	if u.installerPath == "" || u.temporaryDir == "" || filepath.Dir(u.installerPath) != u.temporaryDir || filepath.Base(u.installerPath) != u.Info().InstallerName {
		return errUpdateInvalid
	}
	dir, err := os.Lstat(u.temporaryDir)
	if err != nil || !dir.IsDir() || dir.Mode()&os.ModeSymlink != 0 {
		return errUpdateInvalid
	}
	info, err := os.Lstat(u.installerPath)
	if err != nil || !info.Mode().IsRegular() || info.Size() != u.installerSize {
		return errUpdateInvalid
	}
	f, err := os.Open(u.installerPath)
	if err != nil {
		return errUpdateInvalid
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return errUpdateInvalid
	}
	if u.config.target == "windows-x64" {
		var magic [4]byte
		if _, err := io.ReadFull(f, magic[:]); err != nil || string(magic[:2]) != "MZ" {
			return errUpdateInvalid
		}
	} else {
		if err := validateDMGEnvelope(f, u.installerSize); err != nil {
			return err
		}
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	hash := sha256.New()
	count, err := io.Copy(hash, io.LimitReader(f, u.installerSize+1))
	if err != nil || count != u.installerSize || hex.EncodeToString(hash.Sum(nil)) != u.installerHash {
		return errUpdateInvalid
	}
	return nil
}

func validateDMGEnvelope(f *os.File, size int64) error {
	// The compressed UDIF images we distribute end in a 512-byte, big-endian
	// trailer. A filename or a leading magic value cannot identify these images.
	const trailerSize = 512
	if size <= trailerSize {
		return errUpdateInvalid
	}
	var trailer [trailerSize]byte
	if _, err := f.ReadAt(trailer[:], size-trailerSize); err != nil {
		return errUpdateInvalid
	}
	if string(trailer[:4]) != "koly" || binary.BigEndian.Uint32(trailer[4:8]) != 4 || binary.BigEndian.Uint32(trailer[8:12]) != trailerSize {
		return errUpdateInvalid
	}
	// Segmented disk images are not standalone update installers.
	if binary.BigEndian.Uint32(trailer[56:60]) != 1 || binary.BigEndian.Uint32(trailer[60:64]) != 1 {
		return errUpdateInvalid
	}
	limit := uint64(size - trailerSize)
	for _, field := range []int{24, 40, 216} { // data fork, resource fork, XML plist
		offset := binary.BigEndian.Uint64(trailer[field : field+8])
		length := binary.BigEndian.Uint64(trailer[field+8 : field+16])
		// Subtraction keeps malicious offsets/lengths from wrapping on addition.
		if offset > limit || length > limit-offset || (field != 40 && length == 0) {
			return errUpdateInvalid
		}
	}
	return nil
}

func (u *AppUpdates) Install() UpdateState {
	if !u.begin() {
		return u.Info()
	}
	defer u.operation.Unlock()
	if u.Info().Status != "ready" || u.confirmInstall == nil {
		return u.Info()
	}
	if err := u.validateInstaller(); err != nil {
		u.clearDownload()
		return u.fail(err)
	}
	ctx, cancel := context.WithTimeout(u.ctx, 45*time.Second)
	defer cancel()
	signed, err := u.verifySignature(ctx, u.installerPath, u.Info().Signed)
	if err != nil {
		return u.fail(errUpdateInvalid)
	}
	u.change(func(s *UpdateState) { s.Signed = signed })
	accepted, err := u.confirmInstall(u.Info())
	if err != nil {
		return u.fail(err)
	}
	if !accepted {
		return u.change(func(s *UpdateState) {
			s.Status = "ready"
			s.Message = "설치를 취소했습니다. 다운로드한 파일은 다시 설치할 수 있습니다."
		})
	}
	// A dialog may remain open for a long time. Verify again before handing its
	// exact absolute path to the OS, with no shell expansion or installer flags.
	if u.ctx.Err() != nil {
		return u.fail(u.ctx.Err())
	}
	if err := u.validateInstaller(); err != nil {
		u.clearDownload()
		return u.fail(err)
	}
	if err := u.markOrigin(u.installerPath, u.Info().ReleaseURL); err != nil {
		u.clearDownload()
		return u.fail(errUpdateInvalid)
	}
	if err := u.validateInstaller(); err != nil {
		u.clearDownload()
		return u.fail(err)
	}
	if err := u.launchInstaller(u.installerPath); err != nil {
		return u.change(func(s *UpdateState) {
			s.Status = "ready"
			s.Message = "OS 설치창을 열지 못했거나 실행을 취소했습니다. 다시 시도해 주세요."
			if s.OS == "macos" {
				s.Message = "DMG를 열지 못했거나 실행을 취소했습니다. 다시 시도해 주세요."
			}
		})
	}
	u.launched = true
	state := u.change(func(s *UpdateState) {
		s.Status = "installing"
		s.Message = "OS 설치창을 열었습니다. 설치 후 다시 실행하면 적용된 버전을 확인할 수 있습니다."
		if s.OS == "macos" {
			s.Message = "DMG를 열었습니다. 앱이 종료되면 Huginn을 Applications로 끌어다 놓으세요. 기존 Forkrail 사용자는 Huginn 실행을 확인한 뒤 이전 앱과 Dock 아이콘을 제거해 주세요. 설정은 유지됩니다."
		}
	})
	if u.afterLaunch != nil {
		go u.afterLaunch()
	}
	return state
}

func validUpdateURL(value *url.URL) bool {
	if value.Scheme != "https" || value.User != nil || value.Fragment != "" || value.Opaque != "" || (value.Port() != "" && value.Port() != "443") {
		return false
	}
	switch value.Hostname() {
	case "api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com":
		return true
	}
	return false
}
func (u *AppUpdates) request(ctx context.Context, address, token string, asset bool) (*http.Response, error) {
	response, err := u.requestOnce(ctx, address, token, asset)
	if token != "" && errors.Is(err, errUpdateAuth) {
		return u.requestOnce(ctx, address, "", asset)
	}
	return response, err
}
func (u *AppUpdates) requestOnce(ctx context.Context, address, token string, asset bool) (*http.Response, error) {
	parsed, err := url.Parse(address)
	if err != nil || !validUpdateURL(parsed) || parsed.Host != "api.github.com" {
		return nil, errUpdateInvalid
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return nil, errUpdateInvalid
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	if asset {
		req.Header.Set("Accept", "application/octet-stream")
	}
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "Forkrail-Updater")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	client := *u.client
	client.CheckRedirect = func(next *http.Request, via []*http.Request) error {
		// Go can forward Authorization to subdomains by default. Strip on EVERY hop,
		// including same-host redirects, and allow only GitHub's fixed asset hosts.
		next.Header.Del("Authorization")
		next.Header.Del("Cookie")
		if !asset || len(via) > 5 || !validUpdateURL(next.URL) {
			return errUpdateInvalid
		}
		return nil
	}
	response, err := client.Do(req)
	if err != nil {
		return nil, errors.New("update network request failed")
	}
	if response.StatusCode != http.StatusOK {
		response.Body.Close()
		switch response.StatusCode {
		case 401, 403, 404:
			return nil, errUpdateAuth
		}
		return nil, errors.New("update server unavailable")
	}
	return response, nil
}
func (u *AppUpdates) fetchSmall(ctx context.Context, address, token string, asset bool, limit int64) ([]byte, error) {
	response, err := u.request(ctx, address, token, asset)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.ContentLength > limit {
		return nil, errUpdateInvalid
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, errUpdateInvalid
	}
	return data, nil
}

// The token remains in process memory for this request only. Never use CombinedOutput
// here: CLI diagnostics may contain account information and are not UI messages.
func githubCLIToken(ctx context.Context) string {
	executable, err := exec.LookPath("gh")
	if err != nil && runtime.GOOS == "darwin" {
		for _, path := range []string{"/opt/homebrew/bin/gh", "/usr/local/bin/gh"} {
			if info, e := os.Stat(path); e == nil && info.Mode().IsRegular() {
				executable = path
				break
			}
		}
	}
	if executable == "" {
		return ""
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, executable, "auth", "token", "--hostname", "github.com")
	configureChild(command)
	var output limitedUpdateOutput
	command.Stdout = &output
	if command.Run() != nil {
		return ""
	}
	token := strings.TrimSpace(output.String())
	if len(token) < 1 || len(token) > 4096 || strings.ContainsAny(token, " \t\r\n\x00") {
		return ""
	}
	return token
}

type limitedUpdateOutput struct{ bytes.Buffer }

func (b *limitedUpdateOutput) Write(data []byte) (int, error) {
	if b.Len()+len(data) > 4096 {
		return 0, errors.New("output too large")
	}
	return b.Buffer.Write(data)
}
