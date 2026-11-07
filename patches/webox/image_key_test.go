package wechatdb

import (
	"crypto/aes"
	"crypto/md5"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

func TestWewardRecoverKeyWithoutJPEGThumbnail(t *testing.T) {
	accountID := "wxid_fixture"
	number := "54321"
	suffix := md5.Sum([]byte(number))
	digest := md5.Sum([]byte(number + accountID))
	var expected [16]byte
	copy(expected[:], hex.EncodeToString(digest[:])[:16])
	block, err := aes.NewCipher(expected[:])
	if err != nil {
		t.Fatal(err)
	}
	var template [16]byte
	block.Encrypt(template[:], []byte("wxgf123456789012"))
	key, found := searchImageKey(suffix[:2], accountID, [][16]byte{template}, 1<<16)
	if !found || key.aes != expected || key.xor != byte(54321%256) {
		t.Fatal("image key recovery failed")
	}
	if _, found := searchImageKey(suffix[:2], "another-account", [][16]byte{template}, 1<<16); found {
		t.Fatal("wrong account accepted")
	}
	root := t.TempDir()
	imagePath := filepath.Join(root, "sample_t.dat")
	if err := os.WriteFile(imagePath, append(make([]byte, v2ImageHeaderSize), template[:]...), 0600); err != nil {
		t.Fatal(err)
	}
	saveImageKey(root, key)
	cached, valid := cachedImageKey(root, []v2ImageCandidate{{path: imagePath}})
	if !valid || cached != key {
		t.Fatal("image key cache failed validation")
	}
	info, err := os.Stat(filepath.Join(root, ".weward-image-key.json"))
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("image key cache must be private")
	}
	template[0] ^= 1
	if err := os.WriteFile(imagePath, append(make([]byte, v2ImageHeaderSize), template[:]...), 0600); err != nil {
		t.Fatal(err)
	}
	if _, valid := cachedImageKey(root, []v2ImageCandidate{{path: imagePath}}); valid {
		t.Fatal("stale image key accepted")
	}
}
