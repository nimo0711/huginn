//go:build !darwin

package main

import (
	"context"
	"fmt"

	wailsruntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

func setWaitingBadge(ctx context.Context, count int) {
	title := appName
	if count > 0 {
		title = fmt.Sprintf("%s (%d)", appName, count)
	}
	wailsruntime.WindowSetTitle(ctx, title)
}
