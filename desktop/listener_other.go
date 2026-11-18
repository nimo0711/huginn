//go:build !darwin && !windows

package main

import "context"

// The release shell supports macOS and Windows service ownership checks.
func currentUserListener(_ context.Context, _ int) processIdentity { return processIdentity{} }

func currentUserProcess(_ int) processIdentity { return processIdentity{} }
