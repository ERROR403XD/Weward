package wechatapi

import (
	"errors"
	"testing"
	"time"

	"github.com/netcat-ai/webox/internal/wechatdb"
	"github.com/netcat-ai/webox/wecom"
)

type brokenImageSource struct {
	*fakeMessages
}

func (source brokenImageSource) ReadImage(string, int64, int64, string) (*wechatdb.MediaFile, error) {
	return nil, errors.New("unsupported fixture image")
}

func TestWewardImageFailureDoesNotBlockLaterText(t *testing.T) {
	server, messages, _ := testServer(t)
	server.rooms = brokenImageSource{messages}
	input := []wecom.Message{
		{MsgID: "broken-image", MsgType: wecom.MessageTypeImage, MsgTime: time.Now().Add(-2 * time.Minute).UnixMilli(), Image: &wecom.Image{}},
		{MsgID: "later-text", MsgType: wecom.MessageTypeText, Text: &wecom.Text{Content: "still collected"}},
	}
	ready, blocked, err := server.readyMessages(input)
	if err != nil || blocked || len(ready) != 2 || ready[0].Image.Error == "" || ready[0].Image.SDKFileID != "" || ready[1].MsgID != "later-text" {
		t.Fatalf("ready=%#v blocked=%v err=%v", ready, blocked, err)
	}
	input[0].MsgTime = time.Now().UnixMilli()
	ready, blocked, err = server.readyMessages(input)
	if err != nil || !blocked || len(ready) != 0 {
		t.Fatal("fresh image should still get its bounded retry window")
	}
}
