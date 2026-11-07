package wechatapi

import (
	"errors"
	"net/http"
	"strings"

	"github.com/netcat-ai/webox/internal/wechatdb"
)

func (server *Server) getGroups(response http.ResponseWriter, request *http.Request) {
	if !server.authenticate(response, request) {
		return
	}
	if !server.session.IsInitialized() {
		writeJSON(response, http.StatusOK, sessionUnavailable())
		return
	}
	source, supported := server.contacts.(interface {
		GroupContacts(string) ([]wechatdb.Contact, error)
	})
	if !supported {
		writeError(response, http.StatusNotImplemented, "group search is unavailable")
		return
	}
	contacts, err := source.GroupContacts(request.URL.Query().Get("query"))
	if err != nil {
		writeError(response, http.StatusInternalServerError, "could not query groups")
		return
	}
	writeJSON(response, http.StatusOK, map[string]any{"ret": 0, "contacts": contacts})
}

func validRoomIDs(roomIDs []string) bool {
	for _, roomID := range roomIDs {
		if !strings.HasSuffix(roomID, "@chatroom") || len(roomID) <= len("@chatroom") {
			return false
		}
	}
	return len(roomIDs) <= 1000
}

func (server *Server) sessionsForRooms(roomIDs []string, after int64) (map[string]int64, error) {
	if roomIDs == nil {
		return server.rooms.RoomSessions(after)
	}
	source, supported := server.rooms.(interface {
		RoomSessionsFor([]string, int64) (map[string]int64, error)
	})
	if !supported {
		return nil, errors.New("explicit group selection is unavailable")
	}
	return source.RoomSessionsFor(roomIDs, after)
}
