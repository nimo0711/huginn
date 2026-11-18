package main

import (
	"encoding/binary"
	"testing"
)

func TestWindowsListenerPIDSelection(t *testing.T) {
	table := make([]byte, 4+2*24)
	binary.LittleEndian.PutUint32(table, 1)
	copy(table[8:12], []byte{127, 0, 0, 1})
	binary.BigEndian.PutUint16(table[12:14], 8790)
	binary.LittleEndian.PutUint32(table[24:28], 7381)
	if pid := windowsListenerPID(table, 8790); pid != 7381 {
		t.Fatalf("loopback owner PID = %d, want 7381", pid)
	}
	binary.LittleEndian.PutUint32(table, 2)
	binary.BigEndian.PutUint16(table[36:38], 8790)
	if pid := windowsListenerPID(table, 8790); pid != 0 {
		t.Fatalf("accepted an ambiguous wildcard listener: PID=%d", pid)
	}
	if pid := windowsListenerPID(table[:16], 8790); pid != 0 {
		t.Fatalf("accepted a truncated listener table: PID=%d", pid)
	}
}
