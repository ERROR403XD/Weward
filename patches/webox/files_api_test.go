package wechatapi

import (
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/netcat-ai/webox/internal/wechatdb"
	"github.com/netcat-ai/webox/wecom"
)

type brokenFileSource struct{ *fakeMessages }

func (source brokenFileSource) ReadFile(string, int64, int64, string) (*wechatdb.LocalFile, error) {
	return nil, errors.New("fixture file cannot be read")
}

func fixtureFile(name string) wecom.Message {
	return wecom.Message{MsgID: "file-id", RoomID: "room", MsgType: wecom.MessageTypeFile,
		MsgTime: time.Now().Add(-2 * time.Minute).UnixMilli(), File: &wecom.File{FileName: name}}
}

func TestWewardFilePolicySkipsBeforeReading(t *testing.T) {
	for _, test := range []struct {
		name   string
		policy inboundFilePolicy
		reason string
	}{
		{"disabled", inboundFilePolicy{Enabled: false, MaxBytes: 100}, "disabled"},
		{"extension", inboundFilePolicy{Enabled: true, Extensions: []string{"pdf"}, MaxBytes: 100}, "extension"},
		{"size", inboundFilePolicy{Enabled: true, MaxBytes: 1}, "size"},
	} {
		t.Run(test.name, func(t *testing.T) {
			server, messages, _ := testServer(t)
			server.rooms = brokenFileSource{messages}
			input := fixtureFile("report.TAR.GZ")
			input.MsgTime = time.Now().UnixMilli()
			input.File.FileSize = 10
			ready, blocked, err := server.readyMessagesWithFiles([]wecom.Message{input}, &test.policy)
			if err != nil || blocked || len(ready) != 1 || ready[0].File.SkipReason != test.reason || ready[0].File.Error != "" {
				t.Fatalf("ready=%+v blocked=%v err=%v", ready, blocked, err)
			}
			if input.File.SkipReason != "" {
				t.Fatal("policy mutated source metadata")
			}
		})
	}
}

func TestWewardFilePolicyCopiesAllowedAndRejectsActualOversize(t *testing.T) {
	server, messages, _ := testServer(t)
	path := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(path, []byte("hello"), 0600); err != nil {
		t.Fatal(err)
	}
	messages.files = map[string]*wechatdb.LocalFile{"room/report.TAR.GZ": {Path: path, Filename: "report.TAR.GZ"}}
	policy := &inboundFilePolicy{Enabled: true, Extensions: []string{"tar.gz"}, MaxBytes: 5}
	input := fixtureFile("report.TAR.GZ")
	ready, blocked, err := server.readyMessagesWithFiles([]wecom.Message{input}, policy)
	if err != nil || blocked || len(ready) != 1 || ready[0].File.SDKFileID == "" {
		t.Fatalf("ready=%+v blocked=%v err=%v", ready, blocked, err)
	}
	policy.MaxBytes = 4
	ready, blocked, err = server.readyMessagesWithFiles([]wecom.Message{input}, policy)
	if err != nil || blocked || ready[0].File.SDKFileID != "" || ready[0].File.SkipReason != "size" {
		t.Fatalf("oversize file was copied: %+v %v", ready[0].File, err)
	}
}

func TestWewardFileFailureHasBoundedWait(t *testing.T) {
	for _, broken := range []bool{false, true} {
		server, messages, _ := testServer(t)
		if broken {
			server.rooms = brokenFileSource{messages}
		}
		input := []wecom.Message{fixtureFile("report.pdf"), {MsgID: "later-text", MsgType: wecom.MessageTypeText}}
		ready, blocked, err := server.readyMessagesWithFiles(input, &inboundFilePolicy{Enabled: true, MaxBytes: 100})
		if err != nil || blocked || len(ready) != 2 || ready[0].File.Error == "" || ready[0].File.SDKFileID != "" {
			t.Fatalf("ready=%+v blocked=%v err=%v", ready, blocked, err)
		}
		input[0].MsgTime = time.Now().UnixMilli()
		ready, blocked, err = server.readyMessagesWithFiles(input, &inboundFilePolicy{Enabled: true, MaxBytes: 100})
		if err != nil || !blocked || len(ready) != 0 {
			t.Fatal("fresh missing file must retain the bounded wait")
		}
	}
}

func TestWewardFilePartialDownloadAndMissingMetadata(t *testing.T) {
	server, messages, _ := testServer(t)
	path := filepath.Join(t.TempDir(), "partial")
	if err := os.WriteFile(path, []byte("part"), 0600); err != nil {
		t.Fatal(err)
	}
	messages.files = map[string]*wechatdb.LocalFile{"room/report.pdf": {Path: path, Filename: "report.pdf"}}
	input := fixtureFile("report.pdf")
	input.File.FileSize = 100
	ready, blocked, err := server.readyMessagesWithFiles([]wecom.Message{input}, &inboundFilePolicy{Enabled: true, MaxBytes: 100})
	if err != nil || blocked || ready[0].File.SDKFileID != "" || ready[0].File.Error == "" {
		t.Fatal("partial file must not be published")
	}
	input.File = nil
	ready, blocked, err = server.readyMessagesWithFiles([]wecom.Message{input}, nil)
	if err != nil || blocked || ready[0].File.Error == "" {
		t.Fatal("missing metadata must not panic or stall forever")
	}
}

func TestWewardGetUpdatesUsesAndValidatesFilePolicy(t *testing.T) {
	server, source, _ := testServer(t)
	source.initialized = true
	now := time.Now().Add(-2 * time.Minute).Unix()
	input := fixtureFile("report.pdf")
	input.SortSequence = now*1000 + 100
	source.sessions = map[string]int64{"room": now}
	source.rooms = map[string][]wecom.Message{"room": {input}}
	response := perform(server, "POST", "/ilink/bot/getupdates", map[string]any{
		"cursor": strconv.FormatInt(now*1000-1, 10), "file_policy": inboundFilePolicy{Enabled: false, MaxBytes: 100},
	}, true)
	body := responseJSON(t, response)
	result := body["messages"].([]any)[0].(map[string]any)["file"].(map[string]any)
	if result["skip_reason"] != "disabled" {
		t.Fatalf("policy not passed through HTTP: %v", body)
	}
	response = perform(server, "POST", "/ilink/bot/getupdates", map[string]any{
		"cursor": "0", "file_policy": inboundFilePolicy{Enabled: true, MaxBytes: 0},
	}, true)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("invalid policy accepted: %d", response.Code)
	}
}
