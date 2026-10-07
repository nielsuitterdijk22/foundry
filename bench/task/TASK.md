# Task: TTL LRU cache

Implement package `lru` in `lru/lru.go` (module `bench`, already initialised).

```go
// New returns a cache holding at most capacity entries. Entries expire ttl after
// they were last Put. now is the clock (inject time.Now in production).
func New[K comparable, V any](capacity int, ttl time.Duration, now func() time.Time) *Cache[K, V]

func (c *Cache[K, V]) Put(key K, value V)        // insert or overwrite; refreshes TTL and recency
func (c *Cache[K, V]) Get(key K) (V, bool)       // hit refreshes recency (not TTL); expired = miss and removed
func (c *Cache[K, V]) Len() int                  // number of unexpired entries
func (c *Cache[K, V]) Keys() []K                 // unexpired keys, most recently used first
```

Rules:
- When full, Put evicts expired entries first, then the least recently used.
- capacity <= 0 panics in New.
- Safe for concurrent use.
- Get, Put: O(1).
- Standard library only. Write your own tests in `lru/lru_test.go`. `go vet ./...` and
  `go test -race ./...` must pass.
