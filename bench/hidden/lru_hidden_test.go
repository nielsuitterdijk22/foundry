package lru

import (
	"slices"
	"sync"
	"testing"
	"time"
)

type clock struct{ t time.Time }

func (c *clock) now() time.Time          { return c.t }
func (c *clock) add(d time.Duration)     { c.t = c.t.Add(d) }
func newClock() *clock                   { return &clock{t: time.Unix(1_000_000, 0)} }

func TestHiddenBasic(t *testing.T) {
	c := New[string, int](2, time.Minute, newClock().now)
	c.Put("a", 1)
	c.Put("b", 2)
	if v, ok := c.Get("a"); !ok || v != 1 {
		t.Fatalf("Get(a) = %v, %v", v, ok)
	}
	c.Put("c", 3) // evicts b (a was used more recently)
	if _, ok := c.Get("b"); ok {
		t.Fatal("b should have been evicted")
	}
	if got := c.Keys(); !slices.Equal(got, []string{"c", "a"}) {
		t.Fatalf("Keys() = %v", got)
	}
}

func TestHiddenOverwrite(t *testing.T) {
	c := New[string, int](2, time.Minute, newClock().now)
	c.Put("a", 1)
	c.Put("a", 9)
	if c.Len() != 1 {
		t.Fatalf("Len() = %d", c.Len())
	}
	if v, _ := c.Get("a"); v != 9 {
		t.Fatalf("Get(a) = %d", v)
	}
}

func TestHiddenTTL(t *testing.T) {
	clk := newClock()
	c := New[string, int](3, time.Minute, clk.now)
	c.Put("a", 1)
	clk.add(30 * time.Second)
	c.Get("a") // recency only, not TTL
	clk.add(31 * time.Second)
	if _, ok := c.Get("a"); ok {
		t.Fatal("a should have expired")
	}
	if c.Len() != 0 {
		t.Fatalf("Len() = %d after expiry", c.Len())
	}
	c.Put("b", 2)
	clk.add(59 * time.Second)
	c.Put("b", 3) // refreshes TTL
	clk.add(59 * time.Second)
	if v, ok := c.Get("b"); !ok || v != 3 {
		t.Fatalf("b should be alive with 3, got %v %v", v, ok)
	}
}

func TestHiddenEvictExpiredFirst(t *testing.T) {
	clk := newClock()
	c := New[string, int](2, time.Minute, clk.now)
	c.Put("old", 1)
	clk.add(30 * time.Second)
	c.Put("new", 2)
	c.Get("old") // old is now most recent
	clk.add(31 * time.Second) // old expired, new alive
	c.Put("x", 3) // must evict expired "old", not LRU "new"
	if _, ok := c.Get("new"); !ok {
		t.Fatal("new should survive; expired entries go first")
	}
	if got := c.Keys(); !slices.Equal(got, []string{"new", "x"}) {
		t.Fatalf("Keys() = %v", got)
	}
}

func TestHiddenPanicsOnZeroCapacity(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("New(0) should panic")
		}
	}()
	New[int, int](0, time.Second, time.Now)
}

func TestHiddenConcurrent(t *testing.T) {
	c := New[int, int](64, time.Hour, time.Now)
	var wg sync.WaitGroup
	for g := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range 2000 {
				c.Put((g*31+i)%128, i)
				c.Get(i % 128)
				c.Len()
			}
		}()
	}
	wg.Wait()
	if c.Len() > 64 {
		t.Fatalf("Len() = %d > capacity", c.Len())
	}
}
