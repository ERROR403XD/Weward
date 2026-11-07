package wechat

import "github.com/netcat-ai/webox/internal/wechatdb"

func (state *State) GroupContacts(query string) ([]wechatdb.Contact, error) {
	state.dbMu.Lock()
	defer state.dbMu.Unlock()
	database, _, err := state.readyDatabase()
	if err != nil {
		return nil, err
	}
	return database.GroupContacts(query)
}

func (state *State) RoomSessionsFor(roomIDs []string, after int64) (map[string]int64, error) {
	state.dbMu.Lock()
	defer state.dbMu.Unlock()
	database, _, err := state.readyDatabase()
	if err != nil {
		return nil, err
	}
	return database.RoomSessionsFor(roomIDs, after)
}
