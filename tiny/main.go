package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

const Version = "0.1.1"

type windowDef struct {
	name string
	secs int64
}

var windows = []windowDef{
	{"24h", 24 * 3600},
	{"7d", 7 * 24 * 3600},
	{"30d", 30 * 24 * 3600},
	{"365d", 365 * 24 * 3600},
	{"all", 0},
}

var schemaStatements = []string{
	`CREATE TABLE IF NOT EXISTS toku_usage (
  hour_utc INTEGER NOT NULL,
  project_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT 'unknown',
  requests INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (hour_utc, project_id, model_id, agent)
)`,
	`CREATE TABLE IF NOT EXISTS toku_projects (
  project_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  worktree TEXT
)`,
	`CREATE TABLE IF NOT EXISTS toku_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
)`,
}

const aggSQL = `
INSERT INTO toku_usage
  (hour_utc, project_id, model_id, agent, requests,
   input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens,
   cost, updated_at)
SELECT
  (src.part.time_created / 3600000) * 3600,
  src.session.project_id,
  COALESCE(json_extract(src.message.data, '$.providerID'), 'unknown') || '/' ||
    COALESCE(json_extract(src.message.data, '$.modelID'), 'unknown'),
  COALESCE(json_extract(src.message.data, '$.agent'), 'unknown'),
  COUNT(*),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.input')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.output')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.reasoning')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.cache.read')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.cache.write')), 0),
  COALESCE(SUM(CAST(json_extract(src.part.data, '$.cost') AS REAL)), 0.0),
  ?
FROM src.part
JOIN src.message ON src.message.id = src.part.message_id
JOIN src.session ON src.session.id = src.part.session_id
WHERE json_extract(src.part.data, '$.type') = 'step-finish'
GROUP BY 1, 2, 3, 4
ON CONFLICT (hour_utc, project_id, model_id, agent) DO UPDATE SET
  requests = MAX(requests, excluded.requests),
  input_tokens = MAX(input_tokens, excluded.input_tokens),
  output_tokens = MAX(output_tokens, excluded.output_tokens),
  reasoning_tokens = MAX(reasoning_tokens, excluded.reasoning_tokens),
  cache_read_tokens = MAX(cache_read_tokens, excluded.cache_read_tokens),
  cache_write_tokens = MAX(cache_write_tokens, excluded.cache_write_tokens),
  cost = MAX(cost, excluded.cost),
  updated_at = excluded.updated_at
`

func dataDir() string {
	if v := os.Getenv("TOKU_DATA_DIR"); v != "" {
		return v
	}
	if v := os.Getenv("XDG_DATA_HOME"); v != "" {
		return filepath.Join(v, "toku")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".local", "share", "toku")
}

func tokuDBPath() string {
	if v := os.Getenv("TOKU_DB"); v != "" {
		return v
	}
	return filepath.Join(dataDir(), "toku.db")
}

func snapshotPath() string {
	if v := os.Getenv("TOKU_SNAPSHOT"); v != "" {
		return v
	}
	return filepath.Join(dataDir(), "snapshot.json")
}

func configPath() string {
	return filepath.Join(dataDir(), "config")
}

func loadConfig() map[string]string {
	cfg := map[string]string{}
	if b, err := os.ReadFile(configPath()); err == nil {
		for _, raw := range strings.Split(string(b), "\n") {
			line := strings.TrimSpace(raw)
			if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
				continue
			}
			k, v, _ := strings.Cut(line, "=")
			cfg[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	overrides := [][2]string{
		{"SOURCE_DB", "TOKU_SOURCE_DB"},
		{"SUPABASE_URL", "TOKU_SUPABASE_URL"},
		{"SUPABASE_KEY", "TOKU_SUPABASE_KEY"},
		{"DATABASE_URL", "TOKU_DATABASE_URL"},
	}
	for _, kv := range overrides {
		if v := os.Getenv(kv[1]); v != "" {
			cfg[kv[0]] = v
		}
	}
	return cfg
}

func resolveSourceDB(cfg map[string]string) string {
	if cfg["SOURCE_DB"] != "" {
		return cfg["SOURCE_DB"]
	}
	home, _ := os.UserHomeDir()
	def := filepath.Join(home, ".local", "share", "opencode", "opencode.db")
	if _, err := os.Stat(def); err == nil {
		return def
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "opencode", "db", "path")
	cmd.Stdin = new(bytes.Buffer)
	if out, err := cmd.Output(); err == nil {
		for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
			if _, err := os.Stat(line); err == nil {
				return line
			}
		}
	}
	return def
}

func openDest(dest string) (*sql.DB, error) {
	if dir := filepath.Dir(dest); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	db, err := sql.Open("sqlite", dest)
	if err != nil {
		return nil, err
	}
	for _, stmt := range schemaStatements {
		if _, err := db.Exec(stmt); err != nil {
			db.Close()
			return nil, err
		}
	}
	return db, nil
}

func setMeta(db *sql.DB, key, value string) error {
	_, err := db.Exec(
		"INSERT INTO toku_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
		key, value)
	return err
}

func projectLabel(projectID, name, worktree string) string {
	name = strings.TrimSpace(name)
	if name != "" {
		return name
	}
	wt := strings.TrimSuffix(worktree, "/")
	if wt != "" && wt != "/" {
		base := filepath.Base(wt)
		if base != "" && base != "/" && base != "." {
			return base
		}
	}
	return projectID
}

func refreshProjects(db *sql.DB) (int, error) {
	rows, err := db.Query("SELECT id, COALESCE(name, ''), COALESCE(worktree, '') FROM src.project")
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	type prj struct {
		id, name, worktree string
	}
	var list []prj
	for rows.Next() {
		var p prj
		if err := rows.Scan(&p.id, &p.name, &p.worktree); err != nil {
			return 0, err
		}
		list = append(list, p)
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}
	for _, p := range list {
		if _, err := db.Exec(
			"INSERT OR REPLACE INTO toku_projects (project_id, label, worktree) VALUES (?, ?, ?)",
			p.id, projectLabel(p.id, p.name, p.worktree), p.worktree); err != nil {
			return 0, err
		}
	}
	return len(list), nil
}

type syncStats struct {
	RowsUpserted int64
	RowsTotal    int64
	Projects     int
	Source       string
	Dest         string
}

func syncDB(source, dest string, now int64) (syncStats, error) {
	stats := syncStats{Source: source, Dest: dest}
	if _, err := os.Stat(source); err != nil {
		return stats, fmt.Errorf("source database not found: %s", source)
	}
	db, err := openDest(dest)
	if err != nil {
		return stats, err
	}
	defer db.Close()
	if _, err := db.Exec("ATTACH DATABASE ? AS src", source); err != nil {
		return stats, err
	}
	res, err := db.Exec(aggSQL, now)
	if err != nil {
		return stats, err
	}
	if n, err := res.RowsAffected(); err == nil {
		stats.RowsUpserted = n
	}
	if stats.Projects, err = refreshProjects(db); err != nil {
		return stats, err
	}
	if err := setMeta(db, "last_sync", strconv.FormatInt(now, 10)); err != nil {
		return stats, err
	}
	if err := setMeta(db, "source_db", source); err != nil {
		return stats, err
	}
	if err := setMeta(db, "version", Version); err != nil {
		return stats, err
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM toku_usage").Scan(&stats.RowsTotal); err != nil {
		return stats, err
	}
	return stats, nil
}

type totals struct {
	Requests         int64   `json:"requests"`
	InputTokens      int64   `json:"input_tokens"`
	OutputTokens     int64   `json:"output_tokens"`
	ReasoningTokens  int64   `json:"reasoning_tokens"`
	CacheReadTokens  int64   `json:"cache_read_tokens"`
	CacheWriteTokens int64   `json:"cache_write_tokens"`
	Cost             float64 `json:"cost"`
}

type breakdownRow struct {
	ID               string `json:"id"`
	Label            string `json:"label"`
	Requests         int64  `json:"requests"`
	InputTokens      int64  `json:"input_tokens"`
	OutputTokens     int64  `json:"output_tokens"`
	ReasoningTokens  int64  `json:"reasoning_tokens"`
	CacheReadTokens  int64  `json:"cache_read_tokens"`
	CacheWriteTokens int64  `json:"cache_write_tokens"`
	TotalTokens      int64  `json:"total_tokens"`
}

type windowSnapshot struct {
	Since       *int64         `json:"since"`
	totals
	TotalTokens int64          `json:"total_tokens"`
	ByModel     []breakdownRow `json:"by_model"`
	ByProject   []breakdownRow `json:"by_project"`
	ByAgent     []breakdownRow `json:"by_agent"`
}

type snapshot struct {
	Version     string                     `json:"version"`
	GeneratedAt int64                      `json:"generated_at"`
	Windows     map[string]*windowSnapshot `json:"windows"`
}

func windowTotals(db *sql.DB, secs int64, now int64) (t totals, grandTotal int64, err error) {
	query := "SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(input_tokens), 0), " +
		"COALESCE(SUM(output_tokens), 0), COALESCE(SUM(reasoning_tokens), 0), " +
		"COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_write_tokens), 0), " +
		"COALESCE(SUM(cost), 0.0), " +
		"COALESCE(SUM(input_tokens + output_tokens + reasoning_tokens + cache_read_tokens + cache_write_tokens), 0) " +
		"FROM toku_usage"
	var args []any
	if secs != 0 {
		query += " WHERE hour_utc >= ?"
		args = append(args, now-secs)
	}
	err = db.QueryRow(query, args...).Scan(
		&t.Requests, &t.InputTokens, &t.OutputTokens, &t.ReasoningTokens,
		&t.CacheReadTokens, &t.CacheWriteTokens, &t.Cost, &grandTotal)
	return t, grandTotal, err
}

func breakdown(db *sql.DB, dimension string, secs int64, now int64, limit int64) ([]breakdownRow, error) {
	var query string
	var args []any
	if dimension == "project" {
		query = "SELECT u.project_id, COALESCE(p.label, u.project_id), SUM(u.requests), " +
			"SUM(u.input_tokens), SUM(u.output_tokens), SUM(u.reasoning_tokens), " +
			"SUM(u.cache_read_tokens), SUM(u.cache_write_tokens), " +
			"SUM(u.input_tokens + u.output_tokens + u.reasoning_tokens + u.cache_read_tokens + u.cache_write_tokens) " +
			"FROM toku_usage u LEFT JOIN toku_projects p ON p.project_id = u.project_id "
		if secs != 0 {
			query += "WHERE u.hour_utc >= ? "
			args = append(args, now-secs)
		}
		query += "GROUP BY u.project_id, 2 ORDER BY 9 DESC, 1 LIMIT ?"
	} else {
		query = "SELECT " + dimension + ", " + dimension + ", SUM(requests), " +
			"SUM(input_tokens), SUM(output_tokens), SUM(reasoning_tokens), " +
			"SUM(cache_read_tokens), SUM(cache_write_tokens), " +
			"SUM(input_tokens + output_tokens + reasoning_tokens + cache_read_tokens + cache_write_tokens) " +
			"FROM toku_usage "
		if secs != 0 {
			query += "WHERE hour_utc >= ? "
			args = append(args, now-secs)
		}
		query += "GROUP BY 1, 2 ORDER BY 9 DESC, 1 LIMIT ?"
	}
	args = append(args, limit)
	rows, err := db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []breakdownRow
	for rows.Next() {
		var r breakdownRow
		if err := rows.Scan(&r.ID, &r.Label, &r.Requests, &r.InputTokens, &r.OutputTokens,
			&r.ReasoningTokens, &r.CacheReadTokens, &r.CacheWriteTokens, &r.TotalTokens); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func buildSnapshot(db *sql.DB, now int64) (*snapshot, error) {
	snap := &snapshot{
		Version:     Version,
		GeneratedAt: now,
		Windows:     map[string]*windowSnapshot{},
	}
	for _, w := range windows {
		var since *int64
		if w.secs != 0 {
			s := now - w.secs
			since = &s
		}
		t, total, err := windowTotals(db, w.secs, now)
		if err != nil {
			return nil, err
		}
		win := &windowSnapshot{Since: since, TotalTokens: total}
		win.totals = t
		if win.ByModel, err = breakdown(db, "model_id", w.secs, now, 15); err != nil {
			return nil, err
		}
		if win.ByProject, err = breakdown(db, "project", w.secs, now, 15); err != nil {
			return nil, err
		}
		if win.ByAgent, err = breakdown(db, "agent", w.secs, now, 15); err != nil {
			return nil, err
		}
		snap.Windows[w.name] = win
	}
	return snap, nil
}

func writeSnapshot(path string, snap *snapshot) error {
	b, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	dir := filepath.Dir(path)
	if dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

type supabaseUsageRow struct {
	HourUTC          int64   `json:"hour_utc"`
	ProjectID        string  `json:"project_id"`
	ModelID          string  `json:"model_id"`
	Agent            string  `json:"agent"`
	Requests         int64   `json:"requests"`
	InputTokens      int64   `json:"input_tokens"`
	OutputTokens     int64   `json:"output_tokens"`
	ReasoningTokens  int64   `json:"reasoning_tokens"`
	CacheReadTokens  int64   `json:"cache_read_tokens"`
	CacheWriteTokens int64   `json:"cache_write_tokens"`
	Cost             float64 `json:"cost"`
	UpdatedAt        int64   `json:"updated_at"`
}

type supabaseProjectRow struct {
	ProjectID string `json:"project_id"`
	Label     string `json:"label"`
	Worktree  string `json:"worktree"`
}

func pushSupabase(cfg map[string]string, dest string) (int, int, error) {
	db, err := openDest(dest)
	if err != nil {
		return 0, 0, err
	}
	defer db.Close()
	rows, err := db.Query(
		"SELECT hour_utc, project_id, model_id, agent, requests, input_tokens, output_tokens, " +
			"reasoning_tokens, cache_read_tokens, cache_write_tokens, cost, updated_at FROM toku_usage")
	if err != nil {
		return 0, 0, err
	}
	var usage []supabaseUsageRow
	for rows.Next() {
		var r supabaseUsageRow
		if err := rows.Scan(&r.HourUTC, &r.ProjectID, &r.ModelID, &r.Agent, &r.Requests,
			&r.InputTokens, &r.OutputTokens, &r.ReasoningTokens, &r.CacheReadTokens,
			&r.CacheWriteTokens, &r.Cost, &r.UpdatedAt); err != nil {
			rows.Close()
			return 0, 0, err
		}
		usage = append(usage, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, 0, err
	}
	pRows, err := db.Query("SELECT project_id, label, COALESCE(worktree, '') FROM toku_projects")
	if err != nil {
		return 0, 0, err
	}
	var projects []supabaseProjectRow
	for pRows.Next() {
		var p supabaseProjectRow
		if err := pRows.Scan(&p.ProjectID, &p.Label, &p.Worktree); err != nil {
			pRows.Close()
			return 0, 0, err
		}
		projects = append(projects, p)
	}
	pRows.Close()
	if err := pRows.Err(); err != nil {
		return 0, 0, err
	}

	client := &http.Client{Timeout: 60 * time.Second}
	base := strings.TrimSuffix(cfg["SUPABASE_URL"], "/")
	key := cfg["SUPABASE_KEY"]

	post := func(table, onConflict string, payload any) error {
		body, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		req, err := http.NewRequest("POST",
			base+"/rest/v1/"+table+"?on_conflict="+onConflict,
			bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("apikey", key)
		req.Header.Set("Authorization", "Bearer "+key)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Prefer", "resolution=merge-duplicates")
		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		if resp.StatusCode >= 300 {
			return fmt.Errorf("supabase %s: HTTP %d", table, resp.StatusCode)
		}
		return nil
	}

	for i := 0; i < len(projects); i += 500 {
		end := i + 500
		if end > len(projects) {
			end = len(projects)
		}
		if err := post("toku_projects", "project_id", projects[i:end]); err != nil {
			return 0, 0, err
		}
	}
	for i := 0; i < len(usage); i += 500 {
		end := i + 500
		if end > len(usage) {
			end = len(usage)
		}
		if err := post("toku_usage", "hour_utc,project_id,model_id,agent", usage[i:end]); err != nil {
			return 0, 0, err
		}
	}
	return len(usage), len(projects), nil
}

func peakRSSMB() float64 {
	if runtime.GOOS == "linux" {
		if b, err := os.ReadFile("/proc/self/status"); err == nil {
			for _, line := range strings.Split(string(b), "\n") {
				if strings.HasPrefix(line, "VmHWM:") {
					fields := strings.Fields(line)
					if len(fields) >= 2 {
						if kb, err := strconv.ParseFloat(fields[1], 64); err == nil {
							return kb / 1024
						}
					}
				}
			}
		}
	}
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return float64(m.Sys) / 1e6
}

func human(n int64) string {
	f := float64(n)
	switch {
	case f >= 1e9:
		return fmt.Sprintf("%.1fB", f/1e9)
	case f >= 1e6:
		return fmt.Sprintf("%.1fM", f/1e6)
	case f >= 1e3:
		return fmt.Sprintf("%.1fK", f/1e3)
	}
	return strconv.FormatInt(n, 10)
}

func money(cost float64) string {
	if cost < 0.01 {
		return fmt.Sprintf("$%.4f", cost)
	}
	return fmt.Sprintf("$%.2f", cost)
}

func cmdSync(args []string) error {
	fs := flag.NewFlagSet("sync", flag.ExitOnError)
	sourceFlag := fs.String("source", "", "path to the opencode database (default: auto-detect)")
	dbFlag := fs.String("db", "", "path to toku's own database")
	snapshotFlag := fs.String("snapshot", "", "path to snapshot.json")
	fs.Parse(args)
	cfg := loadConfig()
	source := *sourceFlag
	if source == "" {
		source = resolveSourceDB(cfg)
	}
	dest := *dbFlag
	if dest == "" {
		dest = tokuDBPath()
	}
	snapPath := *snapshotFlag
	if snapPath == "" {
		snapPath = snapshotPath()
	}
	if _, err := os.Stat(source); err != nil {
		fmt.Printf("toku: no opencode database found at %s — nothing to sync\n", source)
		return nil
	}
	now := time.Now().Unix()
	stats, err := syncDB(source, dest, now)
	if err != nil {
		return err
	}
	db, err := openDest(dest)
	if err != nil {
		return err
	}
	defer db.Close()
	snap, err := buildSnapshot(db, now)
	if err != nil {
		return err
	}
	if err := writeSnapshot(snapPath, snap); err != nil {
		return err
	}
	remote := "not configured (local only)"
	if cfg["SUPABASE_URL"] != "" && cfg["SUPABASE_KEY"] != "" {
		usageN, projN, err := pushSupabase(cfg, dest)
		if err != nil {
			fmt.Printf("  remote:   supabase push FAILED: %v\n", err)
		} else {
			remote = fmt.Sprintf("supabase: %d usage rows, %d projects pushed", usageN, projN)
		}
	}
	fmt.Printf("toku sync: %d rows upserted, %d rows total — from %s\n", stats.RowsUpserted, stats.RowsTotal, source)
	fmt.Printf("  snapshot: %s\n", snapPath)
	fmt.Printf("  remote:   %s\n", remote)
	fmt.Printf("  peak_rss: %.1f MB\n", peakRSSMB())
	return nil
}

func cmdReport(args []string) error {
	fs := flag.NewFlagSet("report", flag.ExitOnError)
	dbFlag := fs.String("db", "", "path to toku's own database")
	fs.Parse(args)
	dest := *dbFlag
	if dest == "" {
		dest = tokuDBPath()
	}
	if _, err := os.Stat(dest); err != nil {
		fmt.Println("toku: no data yet — run 'toku sync' first")
		return nil
	}
	db, err := openDest(dest)
	if err != nil {
		return err
	}
	defer db.Close()
	now := time.Now().Unix()
	fmt.Println("toku report — opencode usage, all projects")
	fmt.Printf("generated: %s\n", time.Now().Format("2006-01-02 15:04 MST"))
	fmt.Println()
	fmt.Printf("%-10s %10s %9s %9s %10s %9s %9s %9s %10s\n",
		"WINDOW", "REQUESTS", "INPUT", "OUTPUT", "REASONING", "CACHE-R", "CACHE-W", "TOTAL", "COST")
	var allTotal int64
	for _, w := range windows {
		t, total, err := windowTotals(db, w.secs, now)
		if err != nil {
			return err
		}
		if w.name == "all" {
			allTotal = total
		}
		label := "last " + w.name
		if w.name == "all" {
			label = "all-time"
		}
		fmt.Printf("%-10s %10s %9s %9s %10s %9s %9s %9s %10s\n",
			label, human(t.Requests), human(t.InputTokens), human(t.OutputTokens),
			human(t.ReasoningTokens), human(t.CacheReadTokens), human(t.CacheWriteTokens),
			human(total), money(t.Cost))
	}
	printBreakdown := func(title, dimension string) error {
		fmt.Println()
		fmt.Println(title)
		rows, err := breakdown(db, dimension, 0, now, 10)
		if err != nil {
			return err
		}
		if len(rows) == 0 {
			fmt.Println("  (no data)")
			return nil
		}
		for _, r := range rows {
			share := 0.0
			if allTotal > 0 {
				share = float64(r.TotalTokens) / float64(allTotal) * 100
			}
			bar := strings.Repeat("#", int(share/5+0.5))
			label := r.Label
			if len(label) > 44 {
				label = label[:44]
			}
			fmt.Printf("  %-44s %9s req  %11s tok  %5.1f%% %s\n",
				label, human(r.Requests), human(r.TotalTokens), share, bar)
		}
		return nil
	}
	if err := printBreakdown("BY MODEL (all-time)", "model_id"); err != nil {
		return err
	}
	if err := printBreakdown("BY PROJECT (all-time)", "project"); err != nil {
		return err
	}
	return printBreakdown("BY AGENT (all-time)", "agent")
}

func main() {
	if len(os.Args) < 2 {
		fmt.Println("usage: toku <sync|report> [flags]")
		fmt.Println("  toku sync    aggregate usage + write snapshot (+ optional remote push)")
		fmt.Println("  toku report  print usage windows (24h / 7d / 30d / 365d / all-time)")
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "sync":
		err = cmdSync(os.Args[2:])
	case "report":
		err = cmdReport(os.Args[2:])
	case "--version", "version":
		fmt.Printf("toku %s (go)\n", Version)
	default:
		fmt.Fprintf(os.Stderr, "toku: unknown command %q\n", os.Args[1])
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "toku: %v\n", err)
		os.Exit(1)
	}
}
