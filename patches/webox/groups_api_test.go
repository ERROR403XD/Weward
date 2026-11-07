package wechatapi

import (
	"net/http"
	"testing"

	"github.com/netcat-ai/webox/internal/wechatdb"
)

type explicitGroupMessages struct{ *fakeMessages }

func (source *explicitGroupMessages) GroupContacts(query string) ([]wechatdb.Contact, error) {
	return []wechatdb.Contact{{RoomID: "selected@chatroom", Nickname: query}}, nil
}

func (source *explicitGroupMessages) RoomSessionsFor(roomIDs []string, after int64) (map[string]int64, error) {
	result := map[string]int64{}
	for _, roomID := range roomIDs {
		result[roomID] = 123
	}
	return result, nil
}

func TestGroupAPIRequiresAuthAndExplicitSelection(t *testing.T) {
	server, messages, _ := testServer(t)
	messages.initialized = true
	source := &explicitGroupMessages{messages}
	server.contacts, server.rooms = source, source
	response := perform(server, http.MethodGet, "/ilink/bot/groups?query=test", nil, false)
	if response.Code != http.StatusUnauthorized {
		t.Fatal(response.Code)
	}
	response = perform(server, http.MethodGet, "/ilink/bot/groups?query=test", nil, true)
	body := responseJSON(t, response)
	if response.Code != http.StatusOK || len(body["contacts"].([]any)) != 1 {
		t.Fatalf("body=%v", body)
	}
	response = perform(server, http.MethodPost, "/ilink/bot/getupdates", map[string]any{"cursor": "1000", "room_ids": []string{"private"}}, true)
	if response.Code != http.StatusBadRequest {
		t.Fatal(response.Code)
	}
	sessions, err := server.sessionsForRooms([]string{"selected@chatroom"}, 0)
	if err != nil || len(sessions) != 1 || sessions["selected@chatroom"] != 123 {
		t.Fatalf("sessions=%v err=%v", sessions, err)
	}
	sessions, err = server.sessionsForRooms([]string{}, 0)
	if err != nil || len(sessions) != 0 {
		t.Fatalf("sessions=%v err=%v", sessions, err)
	}
}
