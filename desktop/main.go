package main

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"html/template"
	"net/http"
	"os"
	"path/filepath"
	goruntime "runtime"
	"strconv"
	"sync"
	"time"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/menu"
	"github.com/wailsapp/wails/v2/pkg/menu/keys"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	"github.com/wailsapp/wails/v2/pkg/options/mac"
	"github.com/wailsapp/wails/v2/pkg/options/windows"
	wailsruntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

const appName = "Huginn"
const legacyAppName = "Forkrail"
const bundleID = "app.huginn.desktop"

// scripts/build_release.py supplies the full version from release.json.
var releaseVersion = "1.2.0-rc.1"

//go:embed failure.html
var failureHTML string

//go:embed theme.css
var themeCSS string

var serviceFailurePage = template.Must(template.Must(template.New("service failure").Parse(failureHTML)).New("theme").Parse(themeCSS))

type desktopApp struct {
	config     serviceConfig
	ctx        context.Context
	cancel     context.CancelFunc
	startOnce  sync.Once
	board      *boardService
	updates    *AppUpdates
	proxy      http.Handler
	startErr   error
	runtimeMu  sync.Mutex
	runtimeCtx context.Context
	watchOnce  sync.Once
}

func main() {
	if err := mainRun(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func mainRun(args []string) error {
	var mode string
	if len(args) == 1 {
		switch args[0] {
		case "--smoke-test":
			mode = "service"
		case "--smoke-ui":
			mode = "ui"
		}
	}
	if mode != "" {
		return runSmokeMode(mode)
	}
	if len(args) == 4 && args[0] == applyUpdateFlag {
		pid, err := strconv.Atoi(args[3])
		if err != nil {
			return errUpdateInvalid
		}
		return applyStagedUpdate(args[1], args[2], pid)
	}
	cfg, err := desktopConfig("")
	if err != nil {
		return err
	}
	return runDesktop(cfg, nil, "")
}

func desktopConfig(smokeRoot string) (serviceConfig, error) {
	executable, err := os.Executable()
	if err != nil {
		return serviceConfig{}, err
	}
	parent := filepath.Dir(executable)
	servicePath := filepath.Join(parent, "service", "huginn-service.exe")
	if goruntime.GOOS == "darwin" {
		servicePath = filepath.Join(parent, "..", "Resources", "service", "huginn-service")
	}
	if goruntime.GOOS != "darwin" && goruntime.GOOS != "windows" {
		servicePath = filepath.Join(parent, "service", "huginn-service")
	}

	port := 8790
	if raw := os.Getenv("WORK_BOARD_PORT"); raw != "" {
		port, err = strconv.Atoi(raw)
		if err != nil || port < 1 || port > 65535 {
			return serviceConfig{}, errors.New("WORK_BOARD_PORT는 1~65535 사이의 포트 번호여야 합니다")
		}
	} else if smokeRoot != "" {
		port, err = freeLoopbackPort()
		if err != nil {
			return serviceConfig{}, err
		}
	}

	dataDir := os.Getenv("HUGINN_DATA_DIR")
	explicitState := dataDir != "" || os.Getenv("WORK_BOARD_DB") != "" || os.Getenv("WORK_BOARD_SPOOL") != ""
	if smokeRoot != "" && dataDir == "" {
		dataDir = filepath.Join(smokeRoot, "data")
	}
	if dataDir == "" {
		dataDir, err = defaultDataDir()
		if err != nil {
			return serviceConfig{}, err
		}
	}
	dataDir, err = filepath.Abs(dataDir)
	if err != nil {
		return serviceConfig{}, err
	}

	cfg := serviceConfig{
		port: port, executable: servicePath, dataDir: dataDir, version: releaseVersion,
		readinessTimeout: 20 * time.Second, requireOwned: smokeRoot != "" || explicitState,
	}
	if smokeRoot != "" {
		home := filepath.Join(smokeRoot, "home")
		if err := os.MkdirAll(home, 0700); err != nil {
			return serviceConfig{}, err
		}
		cfg.env = []string{
			"HOME=" + home, "USERPROFILE=" + home,
			"APPDATA=" + filepath.Join(home, "Roaming"), "LOCALAPPDATA=" + filepath.Join(home, "Local"),
			"XDG_DATA_HOME=" + filepath.Join(home, "data"), "XDG_CONFIG_HOME=" + filepath.Join(home, "config"),
			"XDG_CACHE_HOME=" + filepath.Join(home, "cache"), "WORK_BOARD_DB=", "WORK_BOARD_SPOOL=",
		}
	}
	return cfg, nil
}

func defaultDataDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("사용자 데이터 폴더를 찾을 수 없습니다: %w", err)
	}
	switch goruntime.GOOS {
	case "darwin":
		return filepath.Join(home, "Library", "Application Support", appName), nil
	case "windows":
		local := os.Getenv("LOCALAPPDATA")
		if local == "" {
			local = filepath.Join(home, "AppData", "Local")
		}
		return filepath.Join(local, appName), nil
	default:
		return filepath.Join(home, ".local", "share", appName), nil
	}
}

func runDesktop(config serviceConfig, smoke *UISmoke, smokeRoot string) error {
	ctx, cancel := context.WithCancel(context.Background())
	app := &desktopApp{config: config, ctx: ctx, cancel: cancel}
	executable, _ := os.Executable()
	updateCfg, _ := nativeUpdateConfig(executable, config.version)
	app.updates = newAppUpdates(ctx, updateCfg, nil)
	app.updates.confirmInstall = app.confirmUpdateInstall
	app.updates.afterLaunch = func() { app.withRuntime(wailsruntime.Quit) }
	defer app.close()

	opts := &options.App{
		Title: appName, Width: 1180, Height: 860, MinWidth: 420, MinHeight: 400,
		BackgroundColour: options.NewRGB(0x17, 0x17, 0x14), // Matches --bg in theme.css.
		AssetServer:      &assetserver.Options{Handler: app},
		Menu:             app.menu(),
		Mac:              &mac.Options{Appearance: mac.NSAppearanceNameDarkAqua},
		Windows:          &windows.Options{Theme: windows.Dark, WindowClassName: bundleID},
		OnStartup:        app.setRuntimeContext,
		OnDomReady:       app.domReady,
		OnShutdown:       func(context.Context) { app.close() },
		Bind:             []interface{}{app.updates},
	}
	if smoke == nil {
		opts.SingleInstanceLock = &options.SingleInstanceLock{
			UniqueId: bundleID,
			OnSecondInstanceLaunch: func(options.SecondInstanceData) {
				app.withRuntime(func(ctx context.Context) {
					wailsruntime.WindowUnminimise(ctx)
					wailsruntime.WindowShow(ctx)
				})
			},
		}
	} else {
		opts.Windows.WebviewUserDataPath = filepath.Join(smokeRoot, "webview")
		opts.Bind = append(opts.Bind, smoke)
		opts.OnStartup = func(ctx context.Context) {
			app.setRuntimeContext(ctx)
			smoke.startWatchdog(ctx)
		}
		opts.OnDomReady = func(ctx context.Context) {
			smoke.renderProbe(ctx, config.version)
		}
	}
	if err := wails.Run(opts); err != nil {
		return err
	}
	if smoke != nil {
		return smoke.result()
	}
	return nil
}

func (app *desktopApp) start() {
	app.startOnce.Do(func() {
		app.board, app.startErr = startService(app.ctx, app.config)
		if app.startErr == nil {
			nativeOrigin := "wails://wails"
			if goruntime.GOOS == "windows" {
				nativeOrigin = "http://wails.localhost"
			}
			app.proxy = newBoardProxy(app.board, nativeOrigin)
		}
	})
}

func (app *desktopApp) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	app.start()
	if app.proxy != nil {
		app.proxy.ServeHTTP(w, r)
		return
	}
	if r.Method == http.MethodGet && r.URL.Path == "/" && app.startErr != nil {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Security-Policy", "frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
		_ = serviceFailurePage.ExecuteTemplate(w, "service failure", app.startErr.Error())
		return
	}
	http.Error(w, "Huginn 서비스를 시작할 수 없습니다.", http.StatusServiceUnavailable)
}

func (app *desktopApp) close() {
	app.cancel()
	if app.updates != nil {
		app.updates.shutdown()
	}
	app.startOnce.Do(func() {})
	if app.board != nil {
		app.board.stop()
	}
}

func (app *desktopApp) confirmUpdateInstall(state UpdateState) (bool, error) {
	app.runtimeMu.Lock()
	ctx := app.runtimeCtx
	app.runtimeMu.Unlock()
	if ctx == nil {
		return false, errors.New("native dialog unavailable")
	}
	signature := "서명되지 않은 시험 배포입니다. 내려받은 파일의 체크섬과 배포 정보를 확인했습니다."
	if state.Signed {
		signature = "운영체제에서 설치 파일의 서명을 확인했습니다."
	}
	message := "Huginn " + state.LatestVersion + "(으)로 업데이트할까요?\n\n" + signature +
		"\n\n앱을 종료하고 새 버전으로 바꾼 뒤 다시 시작합니다. 설정과 기록은 유지됩니다. 별도로 실행한 서비스는 종료하지 않습니다."
	options, accept := updateInstallDialog(goruntime.GOOS, message)
	answer, err := wailsruntime.MessageDialog(ctx, options)
	return err == nil && answer == accept, err
}

func updateInstallDialog(goos, message string) (wailsruntime.MessageDialogOptions, string) {
	options := wailsruntime.MessageDialogOptions{Type: wailsruntime.QuestionDialog, Title: "Huginn 업데이트 설치", Message: message}
	if goos == "windows" {
		options.DefaultButton = "No"
		return options, "Yes"
	}
	accept := "업데이트 후 다시 시작"
	options.Buttons = []string{accept, "취소"}
	options.DefaultButton = "취소"
	options.CancelButton = "취소"
	return options, accept
}

func (app *desktopApp) setRuntimeContext(ctx context.Context) {
	app.runtimeMu.Lock()
	defer app.runtimeMu.Unlock()
	app.runtimeCtx = ctx
}

func (app *desktopApp) withRuntime(fn func(context.Context)) {
	app.runtimeMu.Lock()
	ctx := app.runtimeCtx
	app.runtimeMu.Unlock()
	if ctx != nil {
		fn(ctx)
	}
}

func (app *desktopApp) domReady(ctx context.Context) {
	app.watchOnce.Do(func() {
		go func() {
			app.start()
			if app.board != nil && app.ctx.Err() == nil {
				watchWaiting(app.ctx, ctx, app.board)
			}
		}()
	})
}

func (app *desktopApp) menu() *menu.Menu {
	result := menu.NewMenu()
	if goruntime.GOOS == "darwin" {
		result.Append(menu.AppMenu())
		result.Append(menu.EditMenu())
	} else {
		file := result.AddSubmenu("파일")
		file.AddText("Huginn 종료", keys.CmdOrCtrl("q"), func(*menu.CallbackData) {
			app.withRuntime(wailsruntime.Quit)
		})
	}
	view := result.AddSubmenu("보기")
	view.AddText("새로 고침", keys.CmdOrCtrl("r"), func(*menu.CallbackData) {
		app.withRuntime(wailsruntime.WindowReload)
	})
	if goruntime.GOOS == "darwin" {
		result.Append(menu.WindowMenu())
	}
	return result
}
