package main

import (
	"bytes"
	_ "embed"
	"fmt"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
)

//go:embed links.js
var linksScript []byte

func newBoardProxy(service *boardService, nativeOrigin string) http.Handler {
	target := service.target
	proxy := &httputil.ReverseProxy{
		Transport: service.transport,
		Rewrite: func(req *httputil.ProxyRequest) {
			req.SetURL(target)
			req.Out.Host = target.Host
			if req.In.Header.Get("Origin") == nativeOrigin {
				req.Out.Header.Set("Origin", target.String())
			}
			// The proxy augments trusted board HTML and needs decoded bytes.
			req.Out.Header.Del("Accept-Encoding")
		},
		ModifyResponse: func(resp *http.Response) error {
			if resp.StatusCode >= 300 && resp.StatusCode < 400 && resp.Header.Get("Location") != "" {
				return fmt.Errorf("unexpected redirect from the local board")
			}
			return prepareBoardPage(resp, target)
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "Huginn 서비스에 연결할 수 없습니다.", http.StatusBadGateway)
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origins := r.Header.Values("Origin")
		if len(origins) > 1 || len(origins) == 1 && origins[0] != nativeOrigin {
			http.Error(w, "Huginn 화면에서 요청해 주세요.", http.StatusForbidden)
			return
		}
		if len(origins) == 0 && r.Method != http.MethodGet && r.Method != http.MethodHead {
			if !trustedWebKitPost(r, nativeOrigin) {
				http.Error(w, "Huginn 화면에서 요청해 주세요.", http.StatusForbidden)
				return
			}
			r = r.Clone(r.Context())
			r.Header.Set("Origin", nativeOrigin)
		}
		if strings.HasPrefix(r.URL.Path, "/doc/") {
			// The default browser renders documents with the backend's sandbox.
			// Native IPC must never share a webview with user document scripts.
			w.Header().Set("Content-Security-Policy", "sandbox")
			http.Error(w, "문서 링크는 기본 브라우저에서 열립니다.", http.StatusForbidden)
			return
		}
		if r.URL.EscapedPath() != r.URL.Path || !allowedBoardRoute(r.Method, r.URL.Path) {
			http.NotFound(w, r)
			return
		}
		if r.URL.Path == "/_huginn/links.js" && r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
			w.Header().Set("X-Content-Type-Options", "nosniff")
			w.Write(linksScript)
			return
		}
		if r.Method == http.MethodPost && !bufferPost(w, r) {
			return
		}
		if !service.available() {
			http.Error(w, "Huginn 서비스에 연결할 수 없습니다.", http.StatusBadGateway)
			return
		}
		proxy.ServeHTTP(w, r)
	})
}

func allowedBoardRoute(method, path string) bool {
	switch method {
	case http.MethodGet:
		switch path {
		case "/", "/stories", "/assets/brand.svg", "/assets/brand.png", "/assets/theme.css", "/_huginn/links.js",
			"/api/health", "/api/state", "/api/sessions", "/api/actions":
			return true
		}
	case http.MethodPost:
		return path == "/api/actions" || path == "/api/alias"
	}
	return false
}

func trustedWebKitPost(r *http.Request, nativeOrigin string) bool {
	// WKWebView omits Origin on writes to a registered custom scheme. Its
	// native scheme and exact trusted board-page referrer identify that source.
	if nativeOrigin != "wails://wails" || r.Method != http.MethodPost || r.Host != "wails" {
		return false
	}
	referrers := r.Header.Values("Referer")
	if len(referrers) != 1 {
		return false
	}
	referrer, err := url.Parse(referrers[0])
	return err == nil && referrer.Scheme == "wails" && referrer.Host == "wails" && referrer.User == nil &&
		(referrer.Path == "/" || referrer.Path == "/stories")
}

func bufferPost(w http.ResponseWriter, r *http.Request) bool {
	// Wails represents WebView uploads as streams. The Python board checks a
	// concrete Content-Length and does not support chunked request bodies.
	const maxBody = 1 << 20
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		http.Error(w, "요청 내용을 읽을 수 없습니다.", http.StatusBadRequest)
		return false
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.Header.Set("Content-Length", strconv.Itoa(len(body)))
	r.TransferEncoding = nil
	return true
}

func prepareBoardPage(resp *http.Response, target *url.URL) error {
	path := resp.Request.URL.Path
	if (path != "/" && path != "/stories") || resp.Request.Method != http.MethodGet ||
		resp.StatusCode != http.StatusOK || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/html") {
		return nil
	}
	defer resp.Body.Close()
	const maxHTML = 2 << 20
	page, err := io.ReadAll(io.LimitReader(resp.Body, maxHTML+1))
	if err != nil {
		return err
	}
	if len(page) > maxHTML || !bytes.Contains(page, []byte("</head>")) {
		return fmt.Errorf("unexpected board HTML")
	}
	injection := fmt.Sprintf(`<script src="/_huginn/links.js" data-board-origin="%s"></script></head>`, target)
	page = bytes.Replace(page, []byte("</head>"), []byte(injection), 1)
	resp.Body = io.NopCloser(bytes.NewReader(page))
	resp.ContentLength = int64(len(page))
	resp.Header.Set("Content-Length", strconv.Itoa(len(page)))
	policy := "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
	if base := resp.Header.Get("Content-Security-Policy"); base != "" {
		policy = base + "; " + policy
	}
	resp.Header.Set("Content-Security-Policy", policy)
	return nil
}
