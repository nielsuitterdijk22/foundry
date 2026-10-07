package main

import "testing"

func TestGreeting(t *testing.T) {
	if got := greeting(); got != "{{name}}" {
		t.Fatalf("greeting() = %q", got)
	}
}
