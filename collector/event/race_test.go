//go:build race

package event

// raceDetector says the tests run with -race, as CI runs them, which slows the regexp engine
// about tenfold.
const raceDetector = true
