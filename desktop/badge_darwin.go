package main

/*
#cgo LDFLAGS: -framework AppKit -framework UniformTypeIdentifiers
void huginn_set_dock_badge(int count);
*/
import "C"

import "context"

func setWaitingBadge(_ context.Context, count int) {
	C.huginn_set_dock_badge(C.int(count))
}
