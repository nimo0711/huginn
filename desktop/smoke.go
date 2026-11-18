package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"

	wailsruntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

//go:embed smoke.js
var smokeScript string

type smokeEvidence struct {
	OK      bool   `json:"ok"`
	Mode    string `json:"mode"`
	Version string `json:"version"`
	Error   string `json:"error,omitempty"`
}

func runSmokeMode(mode string) (runErr error) {
	defer func() {
		evidence := smokeEvidence{OK: runErr == nil, Mode: mode, Version: releaseVersion}
		if runErr != nil {
			evidence.Error = runErr.Error()
		}
		data, _ := json.Marshal(evidence)
		fmt.Println(string(data))
		if path := os.Getenv("FORKRAIL_SMOKE_RESULT"); path != "" {
			if err := os.WriteFile(path, append(data, '\n'), 0600); err != nil {
				runErr = errors.Join(runErr, err)
			}
		}
	}()

	root, err := os.MkdirTemp("", "forkrail-smoke-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(root)
	cfg, err := desktopConfig(root)
	if err != nil {
		return err
	}
	if mode == "service" {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		return smokeService(ctx, cfg)
	}

	return runDesktop(cfg, newUISmoke(), root)
}

// UISmoke is bound to JavaScript only during the isolated WebView smoke check.
type UISmoke struct {
	ctx      context.Context
	ready    chan struct{}
	probe    sync.Once
	finished sync.Once
	resultMu sync.Mutex
	reported bool
	err      error
}

func newUISmoke() *UISmoke {
	return &UISmoke{ready: make(chan struct{})}
}

func (s *UISmoke) startWatchdog(ctx context.Context) {
	s.ctx = ctx
	close(s.ready)
	go func() {
		timer := time.NewTimer(40 * time.Second)
		defer timer.Stop()
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			s.finish(errors.New("WebView smoke timed out"))
		}
	}()
}

func (s *UISmoke) renderProbe(ctx context.Context, version string) {
	s.probe.Do(func() {
		serialized, _ := json.Marshal(version)
		wailsruntime.WindowExecJS(ctx, smokeScript+"("+string(serialized)+");")
	})
}

func (s *UISmoke) Report(message string) {
	if message == "" {
		s.finish(nil)
	} else {
		s.finish(errors.New(message))
	}
}

func (s *UISmoke) finish(err error) {
	<-s.ready
	s.finished.Do(func() {
		s.resultMu.Lock()
		s.reported, s.err = true, err
		s.resultMu.Unlock()
		wailsruntime.Quit(s.ctx)
	})
}

func (s *UISmoke) result() error {
	s.resultMu.Lock()
	defer s.resultMu.Unlock()
	if !s.reported {
		return errors.New("WebView closed without reporting a smoke result")
	}
	return s.err
}
