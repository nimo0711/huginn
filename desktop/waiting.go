package main

import (
	"context"
	"encoding/json"
	"time"

	wailsruntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

type waitingSession struct {
	ID    string `json:"id"`
	Name  string `json:"name"`
	State string `json:"state"`
	Wait  struct {
		Kind string `json:"kind"`
		Text string `json:"text"`
	} `json:"wait"`
}

func watchWaiting(ctx, runtimeCtx context.Context, service *boardService) {
	authorized := make(chan bool, 1)
	go func() {
		if err := wailsruntime.InitializeNotifications(runtimeCtx); err != nil {
			authorized <- false
			return
		}
		wailsruntime.OnNotificationResponse(runtimeCtx, func(result wailsruntime.NotificationResult) {
			if result.Error != nil || ctx.Err() != nil {
				return
			}
			wailsruntime.WindowUnminimise(runtimeCtx)
			wailsruntime.WindowShow(runtimeCtx)
			if sid, ok := result.Response.UserInfo["sid"].(string); ok && sid != "" {
				value, _ := json.Marshal(sid)
				wailsruntime.WindowExecJS(runtimeCtx, "window.location.href = '/?sid=' + encodeURIComponent("+string(value)+");")
			}
		})
		ok, err := wailsruntime.RequestNotificationAuthorization(runtimeCtx)
		authorized <- ok && err == nil
	}()

	tick := time.NewTicker(5 * time.Second)
	defer tick.Stop()
	var previous map[string]struct{}
	notify := false
	for {
		select {
		case notify = <-authorized:
		default:
		}
		next, err := pollWaiting(ctx, service)
		if err == nil && ctx.Err() == nil {
			if previous == nil || len(previous) != len(next) {
				setWaitingBadge(runtimeCtx, len(next))
			}
			current := make(map[string]struct{}, len(next))
			for _, s := range next {
				if _, seen := previous[s.ID]; previous != nil && !seen && notify {
					announceWaiting(runtimeCtx, s)
				}
				current[s.ID] = struct{}{}
			}
			previous = current
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

func pollWaiting(ctx context.Context, service *boardService) ([]waitingSession, error) {
	probe, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	payload, err := service.read(probe, "/api/sessions", 64<<20)
	if err != nil {
		return nil, err
	}
	var snapshot struct {
		Sessions []waitingSession `json:"sessions"`
	}
	if err := json.Unmarshal(payload, &snapshot); err != nil {
		return nil, err
	}
	waiting := make([]waitingSession, 0)
	for _, session := range snapshot.Sessions {
		if session.ID != "" && session.State == "입력 대기" {
			waiting = append(waiting, session)
		}
	}
	return waiting, nil
}

func announceWaiting(ctx context.Context, session waitingSession) {
	name := session.Name
	if name == "" {
		name = "AI 세션"
	}
	kind := map[string]string{
		"ask": "질문", "perm": "권한 승인 대기", "plan": "계획 승인 대기",
		"auto": "자동 모드 차단", "other": "응답 필요",
	}[session.Wait.Kind]
	if kind == "" {
		kind = "나를 기다림"
	}
	body := session.Wait.Text
	if body == "" {
		body = "응답을 기다리고 있습니다"
	}
	_ = wailsruntime.SendNotification(ctx, wailsruntime.NotificationOptions{
		ID: session.ID, Title: name + " " + kind, Body: body, Data: map[string]interface{}{"sid": session.ID},
	})
}
