package wechatdb

import (
	"testing"

	"github.com/netcat-ai/webox/wecom"
)

func TestWewardFileSizeFromMessageMetadata(t *testing.T) {
	var message wecom.Message
	normalizeAppMessage(&message, `<msg><appmsg><type>6</type><title>report.pdf</title><appattach><totallen>42</totallen></appattach></appmsg></msg>`, true)
	if message.File == nil || message.File.FileSize != 42 {
		t.Fatalf("missing declared file size: %+v", message.File)
	}
}
