package main

import "testing"

func TestMacListenerPIDChecksUIDAndEndpoint(t *testing.T) {
	const matching = "p2216\nu501\nf3\nn127.0.0.1:8790\n"
	for _, test := range []struct {
		name, output string
		owner        int
	}{
		{"same user", matching, 2216},
		{"another account", "p2216\nu502\nf3\nn127.0.0.1:8790\n", 0},
		{"wildcard listener", "p2216\nu501\nf3\nn*:8790\n", 0},
		{"ambiguous owner", matching + "p3001\nu501\nf3\nn127.0.0.1:8790\n", 0},
		{"adjacent port", "p2216\nu501\nf3\nn127.0.0.1:8791\n", 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			if pid := macListenerPID(test.output, "8790", 501); pid != test.owner {
				t.Fatalf("pid %d, want %d", pid, test.owner)
			}
		})
	}
}
