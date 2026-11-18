package main

/*
#include <libproc.h>
#include <stdint.h>
#include <sys/proc.h>

static uint64_t forkrail_process_started(int pid, unsigned int uid) {
    struct proc_bsdinfo info;
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) != sizeof(info) ||
        info.pbi_uid != uid || info.pbi_status == SZOMB) {
        return 0;
    }
    return info.pbi_start_tvsec * 1000000 + info.pbi_start_tvusec;
}
*/
import "C"

import (
	"context"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

func currentUserListener(ctx context.Context, port int) processIdentity {
	portText := strconv.Itoa(port)
	output, err := exec.CommandContext(ctx, "/usr/sbin/lsof",
		"-n", "-P", "-a", "-i4TCP:"+portText, "-sTCP:LISTEN", "-Fpun").Output()
	if err != nil {
		return processIdentity{}
	}
	return currentUserProcess(macListenerPID(string(output), portText, os.Geteuid()))
}

func currentUserProcess(pid int) processIdentity {
	if pid <= 0 {
		return processIdentity{}
	}
	started := uint64(C.forkrail_process_started(C.int(pid), C.uint(os.Geteuid())))
	if started == 0 {
		return processIdentity{}
	}
	return processIdentity{pid: pid, started: started}
}

func macListenerPID(output, port string, uid int) int {
	var owner, pid int
	currentUID := -1
	for _, line := range strings.Split(output, "\n") {
		if len(line) < 2 {
			continue
		}
		value := line[1:]
		switch line[0] {
		case 'p':
			pid, _ = strconv.Atoi(value)
			currentUID = -1
		case 'u':
			parsed, err := strconv.Atoi(value)
			if err != nil {
				return 0
			}
			currentUID = parsed
		case 'n':
			if value == "*:"+port {
				return 0
			}
			if value == "127.0.0.1:"+port {
				if pid <= 0 || currentUID != uid || (owner != 0 && owner != pid) {
					return 0
				}
				owner = pid
			}
		}
	}
	return owner
}
