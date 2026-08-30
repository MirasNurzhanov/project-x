// A Go *module* — the rough equivalent of your project's requirements.txt plus
// a package namespace. The module path is how other code would import this; it
// does not have to be a real URL for a service you never publish, but using a
// repo-shaped path is the convention and costs nothing.
module github.com/arcanix/approx/services/go-push

// The minimum Go version this code needs. `net/http`'s method-aware routing
// ("POST /v1/push") landed in 1.22, and this uses it, so 1.22 is the floor.
go 1.23

// There is no `require` block. This service has **zero third-party
// dependencies** — everything below is Go's standard library. That is not
// asceticism for its own sake: it is what makes `go build` produce one static
// binary with nothing to install alongside it, which is the whole deployment
// story in point 4 of your brief.
