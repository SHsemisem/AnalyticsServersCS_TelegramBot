#include <amxmodx>
#include <nvault>
#include <reapi>

#define DAYS_INACTIVITY	7	// Через сколько дней удалять неактивных игроков из nVault

#define MAX_FILENAME_LEN 64

new bool:g_bLogged[MAX_PLAYERS + 1]
new bool:g_bAuthClient[MAX_PLAYERS + 1]

new g_iVaultID

new g_szMonitoring[MAX_PLAYERS + 1][32]

new Array:g_aMsFiles
new Trie:g_tHashes

public plugin_init() {
	register_plugin("Analyzer", "0", "Private")
	
	register_clcmd("__set_pro16", "AuthClientConnected")
	register_clcmd("gsc_user", "GSClientConnected")
	
	RegisterHookChain(RC_FileConsistencyProcess, "FileConsistencyProcess_Post", true)
	RegisterHookChain(RG_CBasePlayer_Spawn, "Spawn_Post", true)
	
	g_aMsFiles = ArrayCreate(MAX_FILENAME_LEN)
	g_tHashes = TrieCreate()
	
	ReadMsHashesFile_Func()
}

public plugin_cfg() {
	g_iVaultID = nvault_open("analyzer")
	
	nvault_prune(g_iVaultID, 0, get_systime() - 86400 * DAYS_INACTIVITY)
}

public client_authorized(pPlayer, const szAuthID[]) {
	if(szAuthID[0] == 'B') {
		g_bLogged[pPlayer] = true
	}
}

public client_disconnected(pPlayer) {
	g_bLogged[pPlayer] = false
	g_bAuthClient[pPlayer] = false
	
	g_szMonitoring[pPlayer][0] = EOS
}

// Команда AuthClient. Вызывается до client_connect()
public AuthClientConnected(pPlayer) {
	g_bAuthClient[pPlayer] = true
	
	g_szMonitoring[pPlayer] = "gs-monitor.com (AC)"
}

// Команда GSClient. Вызывается через пару секунд после FileConsistencyFinal()
public GSClientConnected(pPlayer) {
	read_argv(1, g_szMonitoring[pPlayer], charsmax(g_szMonitoring[]))
	add(g_szMonitoring[pPlayer], charsmax(g_szMonitoring[]), " (GSC)")
}

public Spawn_Post(pPlayer) {
	if(g_bLogged[pPlayer] || !is_user_alive(pPlayer)) {
		return
	}
	
	log_connect(pPlayer)
}

public FileConsistencyProcess_Post(pPlayer, szFileName[], szCmd[], ResourceType:iType, iHash, bool:bIsBreak) {
	if(!iHash || g_bAuthClient[pPlayer] || g_szMonitoring[pPlayer][0] != EOS || ArrayFindString(g_aMsFiles, szFileName) == -1) {
		return
	}
	
	static szHash[9]
	formatex(szHash, charsmax(szHash), "%02X%02X%02X%02X", (iHash) & 0xff, (iHash >> 8) & 0xff, (iHash >> 16) & 0xff, (iHash >> 24) & 0xff)
	
	strtolower(szHash)
	
	if(TrieKeyExists(g_tHashes, szHash)) {
		TrieGetString(g_tHashes, szHash, g_szMonitoring[pPlayer], charsmax(g_szMonitoring[]))
	}
	else {
		log_to_file("unknown.log", "%-50s | %-10s | %n", szFileName, szHash, pPlayer)
	}
}

ReadMsHashesFile_Func() {
	new iFileID = fopen("/addons/rechecker/dlls/ms_hashes.ini", "rt")
	
	new szString[512], szFile[MAX_FILENAME_LEN], szHash[9], szMsName[32]
	
	while(!feof(iFileID)) {
		fgets(iFileID, szString, charsmax(szString))
		
		trim(szString)
		
		if(szString[0] == EOS || szString[0] == ';') {
			continue
		}
		
		parse(szString, szFile, charsmax(szFile), szHash, charsmax(szHash), szMsName, charsmax(szMsName))
		
		strtolower(szHash)
		
		if(!equal(szHash, "any")) {
			TrieSetString(g_tHashes, szHash, szMsName)
		}
		else {
			ArrayPushString(g_aMsFiles, szFile)
			
			RegisterQueryFile(szFile, "MsQueryFile_Handler", RES_TYPE_HASH_ANY)
		}
	}
	
	fclose(iFileID)
}

public MsQueryFile_Handler() {}

log_connect(pPlayer) {
	new szAuthID[24]
	get_user_authid(pPlayer, szAuthID, charsmax(szAuthID))
	
	new szIP[24]
	get_user_ip(pPlayer, szIP, charsmax(szIP), true)
	
	new szTime[24]
	get_time("%Y.%m.%d", szTime, charsmax(szTime))
	
	new szLogPath[64]
	formatex(szLogPath, charsmax(szLogPath), "addons/amxmodx/logs/analyzer/analyzer_%s.log", szTime)
	
	if(g_szMonitoring[pPlayer][0] == EOS) {
		copy(g_szMonitoring[pPlayer], charsmax(g_szMonitoring[]), is_user_steam(pPlayer) ? "Steam" : "Unknown")
	}
	
	log_to_file(szLogPath, "%-24s %-21s %-16s %n", g_szMonitoring[pPlayer], szAuthID, szIP, pPlayer)
	get_time("%H:%M:%S", szTime, charsmax(szTime))
	
	new iNew = !nvault_get(g_iVaultID, szAuthID)
	
	//new iFileID = fopen(szLogPath, "at")
	//fprintf(iFileID, "%s: %s %-28s %-21s %-16s %n^n", szTime, iNew ? "new" : "   ", g_szMonitoring[pPlayer], szAuthID, szIP, pPlayer)
	//fclose(iFileID)
	
	if(iNew) {
		nvault_set(g_iVaultID, szAuthID, "1")
	}
	else {
		nvault_touch(g_iVaultID, szAuthID)
	}
	
	g_bLogged[pPlayer] = true
}