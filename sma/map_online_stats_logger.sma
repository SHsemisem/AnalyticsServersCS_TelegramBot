#include <amxmodx>
#include <fakemeta>

#define PLUGIN "Map Online Stats Logger"
#define VERSION "1.1"
#define AUTHOR "ChatGPT"

new g_player_joins = 0
new g_player_leaves = 0

new g_player_time[33]
new g_player_total[33]

new Float:g_total_online_time = 0.0
new g_online_samples = 0

new g_connected_steamids[128][35]
new g_total_unique = 0

public plugin_init() {
    register_plugin(PLUGIN, VERSION, AUTHOR)
    set_task(60.0, "sample_online", _, _, _, "b")
}

public plugin_end() {
    log_statistics()
	log_statistics_map()
}

public client_authorized(id) {
    static steamid[35]
    get_user_authid(id, steamid, charsmax(steamid))

    if (!is_already_logged(steamid)) {
        copy(g_connected_steamids[g_total_unique], charsmax(g_connected_steamids[]), steamid)
        g_total_unique++
    }

	g_player_joins++
    g_player_time[id] = get_systime()
}

public client_disconnected(id) {

    new Float:timelimit = get_cvar_float("mp_timelimit"); // минуты
    if (timelimit <= 2)
        return
	
	g_player_leaves++

    new join_time = g_player_time[id]
    if (join_time > 0) {
        new duration = get_systime() - join_time
        g_player_total[id] += duration
        g_player_time[id] = 0
    }
}

public sample_online() {
    new online = 0
    for (new i = 1; i <= 32; i++) {
        if (is_user_connected(i)) {
            online++
        }
    }

    g_total_online_time += float(online)
    g_online_samples++
}

public log_statistics() {
    new mapname[32], timestr[64], logfile[128], dir[128]
    get_mapname(mapname, charsmax(mapname))

    formatex(dir, charsmax(dir), "addons/amxmodx/logs/mapstats")
    if (!dir_exists(dir)) {
        mkdir(dir)
    }

    new date[32]
    get_time("%Y-%m-%d", date, charsmax(date))
    formatex(logfile, charsmax(logfile), "addons/amxmodx/logs/mapstats/mapstats_%s.log", date)

    new avg_online = g_online_samples > 0 ? floatround(g_total_online_time / g_online_samples) : 0

    new total_time = 0
    new counted = 0
    for (new i = 1; i <= 32; i++) {
        if (g_player_total[i] > 0) {
            total_time += g_player_total[i]
            counted++
        } else if (is_user_connected(i) && g_player_time[i] > 0) {
            total_time += get_systime() - g_player_time[i]
            counted++
        }
    }

    new avg_time = counted > 0 ? total_time / counted : 0

    get_time("%Y-%m-%d %H:%M:%S", timestr, charsmax(timestr))
    new f = fopen(logfile, "a")
    if (f) {
		fprintf(f, "[%s] Карта: %s^n", timestr, mapname)
		fprintf(f, "  Уникальных игроков: %d^n", g_total_unique)
		fprintf(f, "  Всего подключений: %d^n", g_player_joins)
		fprintf(f, "  Отключений: %d^n", g_player_leaves)
		fprintf(f, "  Средний онлайн: %d игроков^n", avg_online)
		fprintf(f, "  Среднее время игры: %d мин^n", avg_time / 60)
		fprintf(f, "----------------------------------------^n")
        fclose(f)
    }
}

public log_statistics_map() {
    new mapname[32], timestr[64], date[32], logfile[128], dir[128]
    get_mapname(mapname, charsmax(mapname))

    // Создание пути к директории
    formatex(dir, charsmax(dir), "addons/amxmodx/logs/mapstats/%s", mapname)
    if (!dir_exists(dir)) {
        mkdir(dir)
    }

    // Путь к файлу лога
    get_time("%Y-%m-%d", date, charsmax(date))
    formatex(logfile, charsmax(logfile), "%s/%s.log", dir, date)

    // Расчёт статистики
    new avg_online = g_online_samples > 0 ? floatround(g_total_online_time / g_online_samples) : 0

    new total_time = 0
    new counted = 0
    for (new i = 1; i <= 32; i++) {
        if (g_player_total[i] > 0) {
            total_time += g_player_total[i]
            counted++
        } else if (is_user_connected(i) && g_player_time[i] > 0) {
            total_time += get_systime() - g_player_time[i]
            counted++
        }
    }

    new avg_time = counted > 0 ? total_time / counted : 0

    get_time("%Y-%m-%d %H:%M:%S", timestr, charsmax(timestr))
    new f = fopen(logfile, "a")
    if (f) {
        fprintf(f, "[%s] Карта: %s^n", timestr, mapname)
        fprintf(f, "  Уникальных игроков: %d^n", g_total_unique)
        fprintf(f, "  Всего подключений: %d^n", g_player_joins)
        fprintf(f, "  Отключений: %d^n", g_player_leaves)
        fprintf(f, "  Средний онлайн: %d игроков^n", avg_online)
        fprintf(f, "  Среднее время игры: %d мин^n", avg_time / 60)
        fprintf(f, "----------------------------------------^n")
        fclose(f)
    }
}

bool:is_already_logged(const steamid[]) {
    for (new i = 0; i < g_total_unique; i++) {
        if (equal(steamid, g_connected_steamids[i])) {
            return true
        }
    }
    return false
}
