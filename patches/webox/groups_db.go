package wechatdb

import (
	"database/sql"
	"errors"
	"strings"
)

func groupsFromDB(database *sql.DB, query string) ([]Contact, error) {
	rows, err := database.Query(`SELECT username, COALESCE(remark,''), COALESCE(nick_name,'')
		FROM contact WHERE delete_flag=0 AND username LIKE '%@chatroom'
		AND (INSTR(LOWER(COALESCE(nick_name,'')),LOWER(?))>0 OR username=?) ORDER BY nick_name, username`, query, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	contacts := make([]Contact, 0)
	for rows.Next() {
		var contact Contact
		if err := rows.Scan(&contact.RoomID, &contact.Remark, &contact.Nickname); err != nil {
			return nil, err
		}
		contact.Nickname = strings.TrimSpace(contact.Nickname)
		contacts = append(contacts, contact)
	}
	return contacts, rows.Err()
}

func (store *Store) GroupContacts(query string) ([]Contact, error) {
	database, found, err := store.database("contact/contact.db")
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, errors.New("contact database not found")
	}
	return groupsFromDB(database, query)
}

func (store *Store) RoomSessionsFor(roomIDs []string, after int64) (map[string]int64, error) {
	if len(roomIDs) == 0 {
		return map[string]int64{}, nil
	}
	database, found, err := store.database("session/session.db")
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, errors.New("session database not found")
	}
	return loadRoomSessions(database, roomIDs, after)
}
