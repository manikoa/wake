package wakelog

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/transparency-dev/merkle/compact"
	"github.com/transparency-dev/merkle/rfc6962"
	"github.com/transparency-dev/tessera/api"
	"github.com/transparency-dev/tessera/api/layout"
	"github.com/transparency-dev/tessera/client"
	"golang.org/x/mod/sumdb/note"
)

// Reader reads a signed log: DirReader for its directory, or Tessera's
// client.HTTPFetcher for wherever the directory is served, such as a bucket.
type Reader interface {
	ReadCheckpoint(ctx context.Context) ([]byte, error)
	ReadEntryBundle(ctx context.Context, index uint64, p uint8) ([]byte, error)
}

// DirReader reads the log in a local directory.
type DirReader string

func (d DirReader) ReadCheckpoint(context.Context) ([]byte, error) {
	return os.ReadFile(filepath.Join(string(d), layout.CheckpointPath))
}

// ReadEntryBundle reads a bundle, falling back from a partial bundle to the full one: once
// a bundle fills, Tessera deletes its partial versions, which a reader holding an older
// checkpoint still asks for. The full bundle starts with the same entries.
func (d DirReader) ReadEntryBundle(_ context.Context, index uint64, p uint8) ([]byte, error) {
	data, err := os.ReadFile(filepath.Join(string(d), layout.EntriesPath(index, p)))
	if p > 0 && errors.Is(err, fs.ErrNotExist) {
		return os.ReadFile(filepath.Join(string(d), layout.EntriesPath(index, 0)))
	}
	return data, err
}

// What Verify finds when a log doesn't match an earlier checkpoint it was given.
var (
	// ErrCutBack means the log holds fewer entries than an earlier checkpoint signed: its
	// tail is gone, as when an older checkpoint is put back in place of the latest.
	ErrCutBack = errors.New("the log was cut back")
	// ErrRewritten means the entries an earlier checkpoint covers now hash to another root:
	// someone with the log's key signed a different history.
	ErrRewritten = errors.New("the log was rewritten")
)

// Checked is what Verify found in a log.
type Checked struct {
	Origin string
	Size   uint64
	// Checkpoint is the signed checkpoint the log was checked at, as it was read. Keep it
	// where the log's host can't write, and pass it to a later Verify.
	Checkpoint []byte
	// Unsigned are the indexes of entries whose event signature doesn't check under the
	// trust given, or that can't be read as entries at all.
	Unsigned []uint64
}

// Verify checks a log without trusting whoever wrote or serves it: the checkpoint is
// signed by logKey, the entries hash to the checkpoint's root, and each entry's event is
// signed by a signer in trust. each, if not nil, sees every entry in order, with whether
// its signature checked. An error means the log itself doesn't hold together; entries
// that fail their signature check are listed in Checked.Unsigned instead.
//
// One checkpoint alone only says the log agrees with itself: an older checkpoint put back
// passes, with the entries after it gone, and so does a history re-signed with the log's
// key. So earlier takes checkpoints kept from before, as Checked.Checkpoint gave them. Each
// must be signed by logKey, the log must hold at least its entries (ErrCutBack), and those
// entries must hash to its root (ErrRewritten). A checkpoint vouches only for the entries
// it covers, and only as well as it was kept out of reach of whoever holds the log's key.
func Verify(ctx context.Context, r Reader, logKey note.Verifier, trust Trust, each func(index uint64, e Entry, signed bool), earlier ...[]byte) (Checked, error) {
	cp, raw, _, err := client.FetchCheckpoint(ctx, r.ReadCheckpoint, logKey, logKey.Name())
	if err != nil {
		return Checked{}, fmt.Errorf("checkpoint: %w", err)
	}
	out := Checked{Origin: cp.Origin, Size: cp.Size, Checkpoint: raw}
	roots := map[uint64][]byte{}
	for _, data := range earlier {
		before, _, _, err := client.FetchCheckpoint(ctx, func(context.Context) ([]byte, error) { return data, nil }, logKey, logKey.Name())
		if err != nil {
			return out, fmt.Errorf("earlier checkpoint: %w", err)
		}
		if before.Size > cp.Size {
			return out, fmt.Errorf("%w: an earlier checkpoint signed %d entries, and it's at %d", ErrCutBack, before.Size, cp.Size)
		}
		if root, ok := roots[before.Size]; ok && !bytes.Equal(root, before.Hash) {
			return out, fmt.Errorf("%w: two earlier checkpoints sign different roots for %d entries", ErrRewritten, before.Size)
		}
		roots[before.Size] = before.Hash
	}
	tree := (&compact.RangeFactory{Hash: rfc6962.DefaultHasher.HashChildren}).NewEmptyRange(0)
	treeSize := func(context.Context) (uint64, error) { return cp.Size, nil }
	index := uint64(0)
	for bundle, err := range client.EntryBundles(ctx, 4, treeSize, r.ReadEntryBundle, 0, cp.Size) {
		if err != nil {
			return out, fmt.Errorf("entries: %w", err)
		}
		var b api.EntryBundle
		if err := b.UnmarshalText(bundle.Data); err != nil {
			return out, fmt.Errorf("entry bundle %d: %w", bundle.RangeInfo.Index, err)
		}
		first, n := bundle.RangeInfo.First, bundle.RangeInfo.N
		if uint(len(b.Entries)) < first+n {
			return out, fmt.Errorf("entry bundle %d has %d entries, want at least %d", bundle.RangeInfo.Index, len(b.Entries), first+n)
		}
		for _, data := range b.Entries[first : first+n] {
			if err := tree.Append(rfc6962.DefaultHasher.HashLeaf(data), nil); err != nil {
				return out, err
			}
			e, err := ParseEntry(data)
			signed := err == nil && e.Check(trust)
			if !signed {
				out.Unsigned = append(out.Unsigned, index)
			}
			if each != nil {
				each(index, e, signed)
			}
			index++
			if want, ok := roots[index]; ok {
				root, err := tree.GetRootHash(nil)
				if err != nil {
					return out, err
				}
				if !bytes.Equal(root, want) {
					return out, fmt.Errorf("%w: its first %d entries don't hash to the root an earlier checkpoint signed", ErrRewritten, index)
				}
			}
		}
	}
	if index != cp.Size {
		return out, fmt.Errorf("read %d entries, the checkpoint says %d", index, cp.Size)
	}
	root, err := tree.GetRootHash(nil)
	if err != nil {
		return out, err
	}
	if cp.Size == 0 {
		root = rfc6962.DefaultHasher.EmptyRoot()
	}
	if !bytes.Equal(root, cp.Hash) {
		return out, fmt.Errorf("the entries don't hash to the checkpoint's root")
	}
	return out, nil
}
