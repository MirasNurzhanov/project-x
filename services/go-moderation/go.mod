// The Swift AI moderation worker.
//
// Zero third-party dependencies — everything used here is Go's standard
// library. That is a consequence of the design rather than an act of
// discipline: because this service never touches Postgres, it needs no driver,
// and because it never makes a policy decision it needs no shared rule engine.
// It fetches frames, classifies them, and hands the results back.
module github.com/arcanix/approx/services/go-moderation

go 1.23
