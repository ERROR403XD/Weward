package wechatdb

import (
	"database/sql"
	"path/filepath"
	"testing"
)

func TestGroupSearchAndExplicitSessions(t *testing.T) {
	contact, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "contact.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer contact.Close()
	mustExec(t, contact, "CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, delete_flag INTEGER)")
	mustExec(t, contact, "INSERT INTO contact VALUES ('one@chatroom','','同名群',0),('two@chatroom','普通备注','同名群',0),('private','','同名群',0),('gone@chatroom','','同名群',1)")
	groups, err := groupsFromDB(contact, "同名")
	if err != nil || len(groups) != 2 {
		t.Fatalf("groups=%v err=%v", groups, err)
	}
	groups, err = groupsFromDB(contact, "two@chatroom")
	if err != nil || len(groups) != 1 || groups[0].RoomID != "two@chatroom" {
		t.Fatalf("groups=%v err=%v", groups, err)
	}
	session, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "session.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	mustExec(t, session, "CREATE TABLE SessionTable (username TEXT, last_timestamp INTEGER)")
	mustExec(t, session, "INSERT INTO SessionTable VALUES ('one@chatroom',123),('two@chatroom',456),('private',789)")
	store := &Store{dbs: map[string]*sql.DB{"session/session.db": session}}
	sessions, err := store.RoomSessionsFor([]string{"two@chatroom"}, 0)
	if err != nil || len(sessions) != 1 || sessions["two@chatroom"] != 456 {
		t.Fatalf("sessions=%v err=%v", sessions, err)
	}
	sessions, err = store.RoomSessionsFor([]string{}, 0)
	if err != nil || len(sessions) != 0 {
		t.Fatalf("sessions=%v err=%v", sessions, err)
	}
}
