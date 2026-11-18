package main

import (
	"context"
	"encoding/binary"
	"unsafe"

	"golang.org/x/sys/windows"
)

var getExtendedTCPTable = windows.NewLazySystemDLL("iphlpapi.dll").NewProc("GetExtendedTcpTable")

func currentUserListener(ctx context.Context, port int) processIdentity {
	if ctx.Err() != nil {
		return processIdentity{}
	}
	return currentUserProcess(int(windowsListenerPID(ipv4ListenerTable(), port)))
}

func currentUserProcess(pid int) processIdentity {
	if pid <= 0 {
		return processIdentity{}
	}
	process, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION|windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return processIdentity{}
	}
	defer windows.CloseHandle(process)
	var token windows.Token
	if err := windows.OpenProcessToken(process, windows.TOKEN_QUERY, &token); err != nil {
		return processIdentity{}
	}
	defer token.Close()

	user, err := token.GetTokenUser()
	if err != nil {
		return processIdentity{}
	}
	currentUser, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil || !user.User.Sid.Equals(currentUser.User.Sid) {
		return processIdentity{}
	}
	var created, exited, kernel, userTime windows.Filetime
	if err := windows.GetProcessTimes(process, &created, &exited, &kernel, &userTime); err != nil {
		return processIdentity{}
	}
	if status, err := windows.WaitForSingleObject(process, 0); err != nil || status != uint32(windows.WAIT_TIMEOUT) {
		return processIdentity{}
	}
	started := uint64(created.HighDateTime)<<32 | uint64(created.LowDateTime)
	if started == 0 {
		return processIdentity{}
	}
	return processIdentity{pid: pid, started: started}
}

func ipv4ListenerTable() []byte {
	const tcpTableOwnerPIDListener = 3
	const maxTableBytes = 8 << 20
	if getExtendedTCPTable.Find() != nil {
		return nil
	}
	var size uint32
	var table []byte
	// A listener can be opened between querying the table size and reading it.
	for range 3 {
		var ptr unsafe.Pointer
		if len(table) > 0 {
			ptr = unsafe.Pointer(&table[0])
		}
		result, _, _ := getExtendedTCPTable.Call(uintptr(ptr), uintptr(unsafe.Pointer(&size)), 0,
			windows.AF_INET, tcpTableOwnerPIDListener, 0)
		if result == 0 && int(size) <= len(table) {
			return table[:size]
		}
		if result != uintptr(windows.ERROR_INSUFFICIENT_BUFFER) || size < 4 || size > maxTableBytes {
			return nil
		}
		table = make([]byte, size)
	}
	return nil
}

func windowsListenerPID(table []byte, port int) uint32 {
	// MIB_TCPTABLE_OWNER_PID begins with a count, followed by rows of six
	// DWORDs. The local address and port bytes use network byte order.
	const rowBytes = 24
	if len(table) < 4 || uint64(binary.LittleEndian.Uint32(table)) > uint64(len(table)-4)/rowBytes {
		return 0
	}
	count := int(binary.LittleEndian.Uint32(table))
	var owner uint32
	for i := range count {
		row := table[4+i*rowBytes:][:rowBytes]
		if int(binary.BigEndian.Uint16(row[8:10])) != port {
			continue
		}
		switch [4]byte(row[4:8]) {
		case [4]byte{0, 0, 0, 0}:
			return 0
		case [4]byte{127, 0, 0, 1}:
			pid := binary.LittleEndian.Uint32(row[20:24])
			if pid == 0 || (owner != 0 && owner != pid) {
				return 0
			}
			owner = pid
		}
	}
	return owner
}
