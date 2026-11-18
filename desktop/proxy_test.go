package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

func TestProxyTranslatesOnlyNativeOrigin(t *testing.T) {
	var requests atomic.Int32
	var target *url.URL
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Host != target.Host || r.Header.Get("Origin") != target.String() {
			t.Errorf("forwarded Host=%q Origin=%q", r.Host, r.Header.Get("Origin"))
		}
		if r.Header.Get("X-Work-Board-Token") != "existing-token" {
			t.Error("board token was replaced or dropped")
		}
		body, err := io.ReadAll(r.Body)
		if err != nil || string(body) != `{"action":"테스트"}` || r.ContentLength != int64(len(body)) {
			t.Errorf("forwarded body=%q length=%d error=%v", body, r.ContentLength, err)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer backend.Close()
	target, _ = url.Parse(backend.URL)
	proxy := fixtureProxy(t, target, "wails://wails")
	for _, test := range []struct {
		name     string
		origins  []string
		referrer string
		host     string
		status   int
	}{
		{name: "native", origins: []string{"wails://wails"}, status: 204},
		{name: "WebKit board navigation", referrer: "wails://wails/?sid=__board", host: "wails", status: 204},
		{name: "opaque sandbox", origins: []string{"null"}, referrer: "wails://wails/", host: "wails", status: 403},
		{name: "foreign", origins: []string{"https://example.test"}, status: 403},
		{name: "lookalike", origins: []string{"wails://wails.example.test"}, status: 403},
		{name: "backend origin", origins: []string{target.String()}, status: 403},
		{name: "duplicate", origins: []string{"wails://wails", "wails://wails"}, status: 403},
		{name: "missing", host: "wails", status: 403},
		{name: "foreign referrer", referrer: "https://example.test/", host: "wails", status: 403},
		{name: "document referrer", referrer: "wails://wails/doc/index.html", host: "wails", status: 403},
		{name: "wrong webview host", referrer: "wails://wails/", host: "wails.localhost", status: 403},
	} {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/api/actions", strings.NewReader(`{"action":"테스트"}`))
			// Wails constructs requests with a streaming body of unknown length.
			req.ContentLength = -1
			req.Header.Set("X-Work-Board-Token", "existing-token")
			req.Host = test.host
			if test.referrer != "" {
				req.Header.Set("Referer", test.referrer)
			}
			for _, origin := range test.origins {
				req.Header.Add("Origin", origin)
			}
			result := httptest.NewRecorder()
			proxy.ServeHTTP(result, req)
			if result.Code != test.status {
				t.Fatalf("status %d, want %d (%s)", result.Code, test.status, result.Body)
			}
		})
	}
	if got := requests.Load(); got != 2 {
		t.Fatalf("unexpected upstream requests: %d", got)
	}
}

func TestProxyKeepsDocumentsOutOfNativePage(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Origin") != "" {
			t.Error("Origin synthesized for navigation")
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if strings.HasPrefix(r.URL.Path, "/doc/") {
			t.Error("document request reached the upstream from the native webview")
		}
		w.Header().Set("Content-Security-Policy", "frame-ancestors 'none'")
		io.WriteString(w, "<html><head><title>Forkrail</title></head><body>내용</body></html>")
	}))
	defer backend.Close()
	target, _ := url.Parse(backend.URL)
	proxy := fixtureProxy(t, target, "http://wails.localhost")
	document := httptest.NewRecorder()
	proxy.ServeHTTP(document, httptest.NewRequest(http.MethodGet, "/doc/example.html", nil))
	if document.Code != http.StatusForbidden || document.Header().Get("Content-Security-Policy") != "sandbox" {
		t.Fatalf("document is reachable inside the native page: %d %q", document.Code, document.Body)
	}
	board := httptest.NewRecorder()
	proxy.ServeHTTP(board, httptest.NewRequest(http.MethodGet, "/", nil))
	if !strings.Contains(board.Header().Get("Content-Security-Policy"), "frame-ancestors 'none'") || !strings.Contains(board.Body.String(), `src="/_forkrail/links.js"`) {
		t.Fatalf("board policy or link handling missing: %q %q", board.Header().Get("Content-Security-Policy"), board.Body)
	}
}

func TestProxyRejectsUnknownAndAlternateRoutes(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("rejected route reached the backend: %s %s", r.Method, r.URL.EscapedPath())
		w.WriteHeader(http.StatusOK)
	}))
	defer backend.Close()
	target, _ := url.Parse(backend.URL)
	proxy := fixtureProxy(t, target, "http://wails.localhost")
	for _, path := range []string{"//doc/local.html", "/%64oc/local.html", "/api/%68ealth", "/api/unknown"} {
		t.Run(path, func(t *testing.T) {
			result := httptest.NewRecorder()
			proxy.ServeHTTP(result, httptest.NewRequest(http.MethodGet, path, nil))
			if result.Code < 400 {
				t.Fatalf("unexpected status %d", result.Code)
			}
		})
	}
	req := httptest.NewRequest(http.MethodPost, "/api/events", strings.NewReader("{}"))
	req.Header.Set("Origin", "http://wails.localhost")
	result := httptest.NewRecorder()
	proxy.ServeHTTP(result, req)
	if result.Code < 400 {
		t.Fatalf("browser reached the event-ingestion route: %d", result.Code)
	}
}

func fixtureProxy(t *testing.T, target *url.URL, origin string) http.Handler {
	t.Helper()
	port, err := strconv.Atoi(target.Port())
	if err != nil {
		t.Fatal(err)
	}
	owner := currentUserListener(context.Background(), port)
	if owner.pid == 0 {
		t.Fatal("fixture held no identifiable listener")
	}
	service := bindBoardService(target, port, owner, nil)
	t.Cleanup(service.stop)
	return newBoardProxy(service, origin)
}
