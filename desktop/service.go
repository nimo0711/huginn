package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// The existing server predates the versioned health API. Its Signal web icon
// and board-state schema are required before treating that listener as Forkrail.
const legacyIconSHA256 = "2877122ef68df6287532dd95e9b4a7b309adc0b16add46a8a2156537db636377"

var (
	errPortConflict     = errors.New("해당 포트를 다른 서비스가 사용 중입니다")
	errUnexpectedHealth = errors.New("내장 서비스의 응답이 앱과 일치하지 않습니다")
	errServiceChanged   = errors.New("처음 연결한 Forkrail 서비스가 종료되었거나 바뀌었습니다")
)

type serviceConfig struct {
	port             int
	executable       string
	dataDir          string
	version          string
	readinessTimeout time.Duration
	requireOwned     bool
	env              []string
}

type boardService struct {
	target    *url.URL
	owned     *childProcess
	port      int
	owner     processIdentity
	transport *http.Transport
	client    *http.Client
	closed    atomic.Bool
}

type processIdentity struct {
	pid     int
	started uint64
}

type childProcess struct {
	process *os.Process
	done    chan struct{}
	waitErr error
	stopped sync.Once
}

type healthResponse struct {
	OK       bool   `json:"ok"`
	App      string `json:"app"`
	API      int    `json:"api"`
	Version  string `json:"version"`
	Instance string `json:"instance"`
}

func (h healthResponse) currentAPI() bool {
	return h.OK && h.App == "Forkrail" && h.API == 1 && h.Version != ""
}

var loopbackDialer = net.Dialer{Timeout: 2 * time.Second}

var startupClient = newBoardClient(&http.Transport{
	DialContext: loopbackDialer.DialContext, DisableKeepAlives: true,
})

func newBoardClient(transport *http.Transport) *http.Client {
	return &http.Client{
		Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func bindBoardService(target *url.URL, port int, owner processIdentity, child *childProcess) *boardService {
	s := &boardService{target: target, port: port, owner: owner, owned: child}
	s.transport = &http.Transport{
		DialContext:           s.dial,
		ResponseHeaderTimeout: 90 * time.Second,
		IdleConnTimeout:       30 * time.Second,
	}
	s.client = newBoardClient(s.transport)
	return s
}

func (s *boardService) available() bool {
	if s.closed.Load() {
		return false
	}
	if s.owned != nil {
		select {
		case <-s.owned.done:
			return false
		default:
		}
	}
	return true
}

func (s *boardService) samePeer(ctx context.Context) bool {
	if ctx.Err() != nil || !s.available() {
		return false
	}
	if s.owned != nil {
		// The child is already authenticated by its startup nonce. Check its
		// process lifetime without scanning the listening sockets on every poll.
		return s.owner == currentUserProcess(s.owner.pid)
	}
	return s.owner == currentUserListener(ctx, s.port)
}

func (s *boardService) dial(ctx context.Context, _, addr string) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if addr != s.target.Host || !s.samePeer(ctx) {
		return nil, errServiceChanged
	}
	conn, err := loopbackDialer.DialContext(ctx, "tcp4", addr)
	if err != nil {
		return nil, err
	}
	if !s.samePeer(ctx) {
		conn.Close()
		return nil, errServiceChanged
	}
	return conn, nil
}

func freeLoopbackPort() (int, error) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}

func serviceURL(port int) *url.URL {
	return &url.URL{Scheme: "http", Host: net.JoinHostPort("127.0.0.1", strconv.Itoa(port))}
}

func startService(ctx context.Context, cfg serviceConfig) (*boardService, error) {
	target := serviceURL(cfg.port)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	// The reservation only distinguishes an unused port from an occupied one.
	// The subprocess must still return its unique instance to complete startup.
	listener, err := net.Listen("tcp4", target.Host)
	if err != nil {
		if !cfg.requireOwned {
			probeCtx, cancel := context.WithTimeout(ctx, 4*time.Second)
			defer cancel()
			owner := currentUserListener(probeCtx, cfg.port)
			if owner.pid > 0 {
				service := bindBoardService(target, cfg.port, owner, nil)
				if compatibleService(probeCtx, service) {
					return service, nil
				}
				service.stop()
			}
		}
		return nil, fmt.Errorf("%s: %w", target.Host, errPortConflict)
	}
	listener.Close()

	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, fmt.Errorf("내장 서비스 식별자를 만들 수 없습니다: %w", err)
	}
	instance := hex.EncodeToString(nonce[:])
	cmd := exec.Command(cfg.executable, "serve", "--port", strconv.Itoa(cfg.port),
		"--data-dir", cfg.dataDir, "--instance", instance, "--parent-pid", strconv.Itoa(os.Getpid()))
	if len(cfg.env) != 0 {
		cmd.Env = append(os.Environ(), cfg.env...)
	}
	configureChild(cmd)
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("내장 서비스를 실행할 수 없습니다: %w", err)
	}
	child := &childProcess{process: cmd.Process, done: make(chan struct{})}
	go func() {
		child.waitErr = cmd.Wait()
		close(child.done)
	}()

	probeCtx, cancel := context.WithTimeout(ctx, cfg.readinessTimeout)
	defer cancel()
	if err := waitForService(probeCtx, target, child, cfg.version, instance); err != nil {
		child.stop()
		return nil, err
	}
	owner := currentUserListener(probeCtx, cfg.port)
	if owner.pid != child.process.Pid {
		child.stop()
		return nil, errServiceChanged
	}
	return bindBoardService(target, cfg.port, owner, child), nil
}

func waitForService(ctx context.Context, target *url.URL, child *childProcess, version, instance string) error {
	retry := time.NewTicker(100 * time.Millisecond)
	defer retry.Stop()
	for {
		timedProbe, cancel := context.WithTimeout(ctx, 600*time.Millisecond)
		health, err := readHealth(timedProbe, target)
		cancel()
		if err == nil {
			if !health.currentAPI() || health.Version != version || health.Instance != instance {
				return errUnexpectedHealth
			}
			return nil
		}
		select {
		case <-child.done:
			if child.waitErr != nil {
				return fmt.Errorf("내장 서비스가 시작 중 종료되었습니다: %w", child.waitErr)
			}
			return errors.New("내장 서비스가 시작 중 종료되었습니다")
		case <-ctx.Done():
			return fmt.Errorf("내장 서비스 연결 시간을 초과했습니다: %w", ctx.Err())
		case <-retry.C:
		}
	}
}

func (s *boardService) read(ctx context.Context, path string, maxSize int64) ([]byte, error) {
	if !s.available() {
		return nil, errServiceChanged
	}
	return readBoardResponse(ctx, s.client, s.target, path, maxSize)
}

func readBoardResponse(ctx context.Context, client *http.Client, target *url.URL, path string, maxSize int64) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String()+path, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s responded with HTTP %d", path, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxSize+1))
	if err == nil && int64(len(body)) > maxSize {
		return nil, fmt.Errorf("%s exceeded the response size limit", path)
	}
	return body, err
}

func readHealth(ctx context.Context, target *url.URL) (healthResponse, error) {
	var health healthResponse
	body, err := readBoardResponse(ctx, startupClient, target, "/api/health", 4096)
	if err != nil {
		return health, err
	}
	err = json.Unmarshal(body, &health)
	return health, err
}

func compatibleService(ctx context.Context, service *boardService) bool {
	data, err := service.read(ctx, "/api/health", 4096)
	var health healthResponse
	if err != nil || json.Unmarshal(data, &health) != nil || !health.OK {
		return false
	}
	if health.currentAPI() {
		return true
	}
	if health.App != "" || health.API != 0 || health.Version != "" || health.Instance != "" {
		return false
	}

	icon, err := service.read(ctx, "/assets/brand.png", 512<<10)
	if err != nil || fmt.Sprintf("%x", sha256.Sum256(icon)) != legacyIconSHA256 {
		return false
	}
	body, err := service.read(ctx, "/api/state?limit=1", 8<<20)
	if err != nil {
		return false
	}
	var state struct {
		Now      *float64
		Story    *string
		Stories  []json.RawMessage
		Items    []json.RawMessage
		Metrics  []json.RawMessage
		Timeline []json.RawMessage
		Sessions []json.RawMessage
	}
	return json.Unmarshal(body, &state) == nil &&
		state.Now != nil && state.Story != nil && state.Stories != nil && state.Items != nil &&
		state.Metrics != nil && state.Timeline != nil && state.Sessions != nil
}

func (s *boardService) stop() {
	s.closed.Store(true)
	s.transport.CloseIdleConnections()
	if s.owned != nil {
		s.owned.stop()
	}
}

func (child *childProcess) stop() {
	child.stopped.Do(func() {
		select {
		case <-child.done:
			return
		default:
		}
		if signalChild(child.process) == nil {
			grace := time.NewTimer(2 * time.Second)
			defer grace.Stop()
			select {
			case <-child.done:
				return
			case <-grace.C:
			}
		}
		_ = child.process.Kill()
		<-child.done
	})
}

func smokeService(ctx context.Context, cfg serviceConfig) error {
	cfg.requireOwned = true
	service, err := startService(ctx, cfg)
	if err != nil {
		return err
	}
	defer service.stop()

	probeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	page, err := service.read(probeCtx, "/", 2<<20)
	if err != nil {
		return err
	}
	if !bytes.Contains(page, []byte("<title>Forkrail</title>")) || !bytes.Contains(page, []byte(`id="main"`)) {
		return errors.New("내장 서비스의 보드 화면을 확인할 수 없습니다")
	}
	return nil
}
