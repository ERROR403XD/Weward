package wechatdb

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
)

var wewardImageKeyMu sync.Mutex

type savedImageKey struct {
	AES [16]byte `json:"aes"`
	XOR byte     `json:"xor"`
}

func cachedImageKey(accountDir string, files []v2ImageCandidate) (imageKeyMaterial, bool) {
	data, err := os.ReadFile(filepath.Join(accountDir, ".weward-image-key.json"))
	if err != nil {
		return imageKeyMaterial{}, false
	}
	var saved savedImageKey
	if json.Unmarshal(data, &saved) != nil {
		return imageKeyMaterial{}, false
	}
	templates, err := imageTemplateBlocks(files, 3)
	return imageKeyMaterial{aes: saved.AES, xor: saved.XOR}, err == nil && verifyImageKey(saved.AES, templates)
}

func saveImageKey(accountDir string, key imageKeyMaterial) {
	data, err := json.Marshal(savedImageKey{AES: key.aes, XOR: key.xor})
	if err != nil {
		return
	}
	file, err := os.CreateTemp(accountDir, ".weward-image-key-")
	if err != nil {
		return
	}
	defer os.Remove(file.Name())
	_, writeErr := file.Write(data)
	closeErr := file.Close()
	if writeErr == nil && closeErr == nil {
		_ = os.Rename(file.Name(), filepath.Join(accountDir, ".weward-image-key.json"))
	}
}

func recoverImageKey(accountDir string, files []v2ImageCandidate) (imageKeyMaterial, error) {
	templates, err := imageTemplateBlocks(files, 3)
	if err != nil {
		return imageKeyMaterial{}, err
	}
	raw := filepath.Base(accountDir)
	separator := strings.LastIndexByte(raw, '_')
	if separator < 1 || len(raw)-separator-1 != 4 {
		return imageKeyMaterial{}, errors.New("account directory has no four-hex suffix")
	}
	suffix, err := hex.DecodeString(raw[separator+1:])
	if err != nil {
		return imageKeyMaterial{}, err
	}
	for _, accountID := range []string{raw[:separator], raw} {
		if key, found := searchImageKey(suffix, accountID, templates, 1<<32); found {
			return key, nil
		}
	}
	return imageKeyMaterial{}, errors.New("could not recover the local WeChat image key")
}

func searchImageKey(suffix []byte, accountID string, templates [][16]byte, limit uint64) (imageKeyMaterial, bool) {
	workers := min(max(runtime.GOMAXPROCS(0), 1), 24)
	var stopped atomic.Bool
	var group sync.WaitGroup
	result := make(chan imageKeyMaterial, 1)
	for worker := 0; worker < workers; worker++ {
		start, end := limit*uint64(worker)/uint64(workers), limit*uint64(worker+1)/uint64(workers)
		group.Add(1)
		go func() {
			defer group.Done()
			var numberBuffer [20]byte
			for candidate := start; candidate < end && !stopped.Load(); candidate++ {
				number := strconv.AppendUint(numberBuffer[:0], candidate, 10)
				digest := md5.Sum(number)
				if digest[0] != suffix[0] || digest[1] != suffix[1] {
					continue
				}
				aesDigest := md5.Sum([]byte(string(number) + accountID))
				var encoded [32]byte
				hex.Encode(encoded[:], aesDigest[:])
				var key [16]byte
				copy(key[:], encoded[:16])
				if verifyImageKey(key, templates) && stopped.CompareAndSwap(false, true) {
					result <- imageKeyMaterial{aes: key, xor: byte(candidate)}
				}
			}
		}()
	}
	group.Wait()
	close(result)
	key, found := <-result
	return key, found
}
