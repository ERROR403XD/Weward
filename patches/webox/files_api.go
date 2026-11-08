package wechatapi

import (
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/netcat-ai/webox/internal/wechatdb"
	"github.com/netcat-ai/webox/wecom"
)

type inboundFilePolicy struct {
	Enabled    bool     `json:"enabled"`
	Extensions []string `json:"extensions"`
	MaxBytes   int64    `json:"max_bytes"`
}

var errInboundFileTooLarge = errors.New("file exceeds configured size limit")
var fileExtensionPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)*$`)

func validFilePolicy(policy *inboundFilePolicy) bool {
	if policy == nil {
		return true
	}
	if policy.MaxBytes <= 0 || policy.MaxBytes > 32*1024*1024 || len(policy.Extensions) > 100 {
		return false
	}
	for _, extension := range policy.Extensions {
		if len(extension) > 40 || !fileExtensionPattern.MatchString(extension) {
			return false
		}
	}
	return true
}

func (policy *inboundFilePolicy) skipReason(filename string, size int64) string {
	if policy == nil {
		return ""
	}
	if !policy.Enabled {
		return "disabled"
	}
	if len(policy.Extensions) != 0 {
		allowed := false
		for _, extension := range policy.Extensions {
			if strings.HasSuffix(strings.ToLower(filename), "."+extension) {
				allowed = true
				break
			}
		}
		if !allowed {
			return "extension"
		}
	}
	if size > policy.MaxBytes {
		return "size"
	}
	return ""
}

func (server *Server) materializePolicyFile(roomID string, message wecom.Message, policy *inboundFilePolicy) (*wechatdb.LocalFile, string, error) {
	if policy == nil {
		return server.materializeInboundFile(roomID, message)
	}
	if server.media == nil {
		return nil, "", errors.New("shared media store is unavailable")
	}
	file, err := server.rooms.ReadFile(roomID, message.Sequence, time.UnixMilli(message.MsgTime).Unix(), message.File.FileName)
	if err != nil {
		return nil, "", fmt.Errorf("read WeChat file: %w", err)
	}
	if file == nil {
		return nil, "", errInboundMediaNotReady
	}
	info, err := os.Stat(file.Path)
	if err != nil {
		return nil, "", err
	}
	if !info.Mode().IsRegular() {
		return nil, "", errors.New("incoming file is not a regular file")
	}
	if info.Size() > policy.MaxBytes {
		return nil, "", errInboundFileTooLarge
	}
	if message.File.FileSize > 0 && info.Size() != message.File.FileSize {
		return nil, "", errors.New("file download is incomplete")
	}
	source, err := os.Open(file.Path)
	if err != nil {
		return nil, "", err
	}
	defer source.Close()
	// Bound the read even if the client is still appending to the local file.
	content, err := io.ReadAll(io.LimitReader(source, policy.MaxBytes+1))
	if err != nil {
		return nil, "", err
	}
	if int64(len(content)) > policy.MaxBytes {
		return nil, "", errInboundFileTooLarge
	}
	if int64(len(content)) != info.Size() {
		return nil, "", errors.New("file changed during collection")
	}
	sharedPath, err := server.media.WriteInboxFile(roomID, message.MsgID, file.Filename, content)
	if err != nil {
		return nil, "", fmt.Errorf("write WeChat file to shared directory: %w", err)
	}
	return file, sharedPath, nil
}
