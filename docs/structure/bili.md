# bili unit

> 鎬ц川锛氱粨鏋勮璁°€傛湰鏂囨弿杩?`bili` unit銆?
> 鏈锛?*`A 鏈嶅姟 B` 鈮?A 鏄熀纭€锛孊 璋冪敤 A**锛堟湇鍔℃柟鍚?A鈫払锛岃皟鐢ㄦ柟鍚?B鈫扐锛夈€?

## 0. 椤圭洰瀹氫綅

bili_unit 鏄嫭绔嬬殑 Bilibili 鐢ㄦ埛鏁版嵁鎸佷箙鍖栧崟鍏冦€傚畠閫氳繃 CLI / command
瀹屾垚鎶撳彇銆佽В鏋愩€佸鐞嗭紝骞舵妸姣忎釜 uid 鐨勭粨鏋滆惤鎴?SQLite 鏂囦欢锛涜渚х敱璋冪敤鏂?
鐩存帴浣跨敤 SQL 鏌ヨ銆傛湰鏂囧彧鎻忚堪鏈粨搴撳唴閮ㄧ粨鏋勪笌杈圭晫銆?

## 1. 浣嶇疆

```text
Bilibili 澶栭儴婧?鈫?bili_unit 鈫?SQLite 鏁版嵁鏂囦欢 / 瀹夸富搴旂敤
```

```text
澶栭儴婧?   Bilibili API / CDN
鏍稿績鍗曚綅  鐩爣鐢ㄦ埛 uid
杈撳嚭褰㈡€? per-uid SQLite main DB + raw DB + workdir
```

## 2. 瀹氫綅

```text
瀵硅薄   Bilibili 鏁版嵁婧?
鍗曚綅   鐩爣鐢ㄦ埛 uid
杈撳叆   鐩爣鐢ㄦ埛 uid + 璁よ瘉淇℃伅 + 浠诲姟
杈撳嚭   澶勭悊缁撴灉涓庣姸鎬?
鏈嶅姟   CLI / Python 瀹夸富搴旂敤 / SQL 璇诲彇鏂?
```

## 3. 绠＄嚎

```text
鎶撳彇 鈫?瑙ｆ瀽 鈫?澶勭悊
```

```text
鎶撳彇   璁よ瘉 鈫?璋冪敤 Bilibili API 鈫?鍘熷鏁版嵁鍏ュ簱
瑙ｆ瀽   璇诲彇鍘熷鏁版嵁 鈫?瀵硅薄鍖栦负 typed dataclass 鈫?鍙€夊浘鐗囦笅杞?鈫?typed object 鍏ュ簱
澶勭悊   璇诲彇 typed object 鈫?ASR 杞綍 鈫?澶勭悊缁撴灉鍏ュ簱
```

## 4. 妯″潡

### 鎶撳彇

```text
auth         鑾峰彇 / 鏍￠獙 / 鎻愪緵鍙敤璁よ瘉锛涜璇佸紓甯稿啓鍏?stage_error
env          淇濆瓨璁よ瘉閰嶇疆锛堜笌 unit-level _env 鍏变韩锛?
client       鎶撳彇鑴氭湰锛汣redential 鐢?auth 鎻愪緵锛涗緷鎹?bilibili-api-python 涓庢湰椤圭洰 endpoint catalog 璋冪敤涓婃父鑳藉姏
rate_limit   鎺у埗璇锋眰棰戠巼涓庡苟鍙戯紱闄愭祦鐘舵€佽繘绋嬪唴椹荤暀锛岄檺娴佸紓甯稿啓鍏?stage_error
_store       FetchingStore锛涘啓 raw_payload + fetch_progress锛坮aw DB锛? stage_task / fetch_endpoint_state / stage_error锛坢ain DB锛?
runner       鏍规嵁 stage_task / fetch_endpoint_state / stage_error 缂栨帓鎶撳彇鎵ц / 閲嶈瘯
```

### 瑙ｆ瀽

```text
models       6 涓?typed dataclass锛圲pProfile / VideoDetail / VideoSubtitle / Article / OpusPost / DynamicPost锛夛紱from_raw() / to_dict() / from_dict() + 鍥剧墖鍗忚銆傝法 model 鍏变韩鐨?SourceRef / CrossRefs 钀藉湪 models/_refs.py銆?
_images      ImageDownloader锛沘iohttp 骞跺彂涓嬭浇 + skip-existing + 澶辫触闅旂
specs        ParsingSpec registry锛汳ODEL_ORDER锛沵aterializer_handler 鍒嗗彂
materializer ParsingMaterializer锛沺er-model raw 鈫?typed 鈫?save_*
_store       ParsingStore锛涘啓涓?DB 鐨?6 寮犲唴瀹硅〃 + image_asset + stage_task[stage='parsing']
command      ParsingCommand锛沺arse_uid() 缂栨帓 6 涓?model + 鍙€夊浘鐗囦笅杞?
```

### 澶勭悊

```text
audio        闊抽涓嬭浇 + ASR 杞綍閫昏緫锛涜皟鐢ㄥ閮?CDN 涓?ASR 寮曟搸锛堝鐞嗛樁娈靛敮涓€澶栭儴璋冪敤妯″潡锛屼緷鎹?unit 搂3 鏄惧紡鐧昏锛?
_store       ProcessingStore锛涘啓涓?DB 鐨?audio_transcription + stage_task[stage='processing'] + stage_error[stage='processing']
runner       鏍规嵁 stage_task / audio_transcription / stage_error 缂栨帓澶勭悊鎵ц / 閲嶈瘯锛涢┍鍔?audio pipeline
command      ProcessingCommand锛沺rocess_uid() 缂栨帓 audio pipeline + retry
```

```text
璺ㄦ簮褰掍竴鍖?/ 娓呮礂涓嶅湪 bili unit 鍐呴儴瀹屾垚銆?
  bili.processing 浠呬骇鍑?Bilibili 鏉ユ簮鐨勭粨鏋勫寲鏁版嵁涓庨煶棰戣浆褰曪紱
  鏇翠笂灞傜殑鍒嗘瀽銆佺储寮曘€佹绱㈢敱璋冪敤鏂瑰湪鏈」鐩箣澶栧畬鎴愩€?
```

### 瀛樺偍

```text
_db          SQLite 鎸佷箙鍖栧眰锛坧aths / connection / context / DDL锛夛紱鎸?uid 娲剧敓 main DB / raw DB / workdir
main DB      {bili_db_dir}/{uid}.db 鈥斺€?娑堣垂鏂瑰绾︼紱6 寮犲唴瀹硅〃 + image_asset + stage_task / fetch_endpoint_state / stage_error + manifest_summary / video_full views
raw DB       {bili_db_dir}/{uid}.raw.db 鈥斺€?producer-private锛況aw_payload + fetch_progress
workdir      {bili_db_dir}/{uid}/ 鈥斺€?images锛坧arsing 涓嬭浇锛? audio temp & ASR cache锛坧rocessing 涓棿浜х墿锛夛紱DB 鍐呭彧瀛樼浉瀵硅矾寰?
```

```text
SQLite 鏄敮涓€绋冲畾 deliverable锛涙秷璐规柟鐢?stdlib sqlite3 鐩磋繛涓?DB 鏌?SQL銆?
鍏蜂綋琛ㄤ笌瀛楁璇箟瑙?docs/schema.md锛岃惤鐩樻牸寮忚 docs/feature/*.md 涓?docs/structure/fetching-contract.md銆?
```

### 鍏ュ彛

```text
command      鍐欎晶鍏ュ彛锛涢┍鍔ㄦ姄鍙栥€佽В鏋愪笌澶勭悊绠＄嚎
璇讳晶         娑堣垂鏂圭洿鎺?sqlite3.connect(bili_unit.db_path(uid))锛涗笉鍐嶆湁 Python query facade
```

## 5. 绠＄嚎瀵硅薄

```text
鏉ユ簮   bilibili-api-python锛圢emo2011/bilibili-api锛? 鏈」鐩?endpoint catalog
鑼冨洿   鍜岀洰鏍囩敤鎴?uid 鏈夊叧鐨勮鍙栨帴鍙ｏ紙uid-level锛夛紝浠ュ強浠?uid 鎶撳彇缁撴灉娲剧敓鐨?item-level 璇诲彇鎺ュ彛
鍗曚綅   鐩爣鐢ㄦ埛 uid
鍒嗙被   濡備笅
```

```text
User(uid)
鐢ㄦ埛鍩虹淇℃伅           鈫?parsing
鐢ㄦ埛鍙戝竷鍐呭锛堣棰戯級   鈫?parsing + audio
鐢ㄦ埛鍙戝竷鍐呭锛堟枃绔狅級   鈫?parsing
鐢ㄦ埛绌洪棿鍐呭           鈫?parsing
鐢ㄦ埛鍏崇郴鍐呭           鈫?parsing
鐢ㄦ埛鍒楄〃鍐呭           鈫?parsing
鐢ㄦ埛鐘舵€?/ 缁熻鍐呭    鈫?parsing
```

## 6. 鏁版嵁娴?

Current data flow:

```text
fetching -> raw.db
parsing  -> raw.db -> main.db
asr      -> main.db
```

`raw.db` is input truth for parsing. `main.db` is materialized truth plus ASR state. The ASR pipeline discovers work from `main.db.video` + `main.db.video_page` and only uses `main.db.video_subtitle` for subtitle short-circuiting. It does not open or read `raw.db`.

```text
fetching.command  鈫?fetching.runner 鈫?auth 鈫?_env
                                    鈫?client 鈫?rate_limit
                                    鈫?FetchingStore 鈫?_db.UidContext
                                                       鈹溾攢 raw DB (raw_payload + fetch_progress)
                                                       鈹斺攢 main DB (stage_task + fetch_endpoint_state + stage_error)
parsing.command   鈫?parsing.materializer 鈫?FetchingStore (read raw_payload)
                                          鈫?models[*].from_raw() 鈫?typed object
                                          鈫?ImageDownloader锛堝彲閫夛級鈫?workdir/images/
                                          鈫?ParsingStore 鈫?main DB (user_profile / video / video_subtitle /
                                                                     article / opus_post / dynamic_event /
                                                                     video_page / image_asset / stage_task)
processing.command 鈫?processing.runner 鈫?audio
                                       鈫?FetchingStore (read raw_payload for video_detail CDN URL)
                                       鈫?ParsingStore (read video / video_subtitle for cid / 瀛楀箷鐭矾)
                                       鈫?ProcessingStore 鈫?main DB (audio_transcription + stage_task + stage_error)
                                       鈫?workdir/audio/ (temp + ASR cache; 鏀跺熬鍚庢竻鐞?
璋冪敤鏂?鈫?sqlite3.connect(db_path(uid)) 鈫?SELECT 鍐呭琛?/ video_full / manifest_summary view
```

```text
bili 涓诲姩璋冪敤 Bilibili 澶栭儴婧?
fetching 閫氳繃 raw_payload 琛屽悜 parsing / processing 鏆撮湶 raw 鏁版嵁锛涗笉鐩存帴鍏变韩 dataclass
audio 涓诲姩璋冪敤澶栭儴婧愶紙CDN 涓嬭浇銆丄SR 杞綍锛夛紱鍏朵綑妯″潡涓嶈皟鐢ㄥ閮?API
璋冪敤鏂归€氳繃 SQL 鍙璁块棶 main DB锛涗笉鎵撳紑 raw DB锛堥櫎闈炶 re-parse锛?
```

## 7. 鐘舵€佸綊灞?

```text
鐩爣鐢ㄦ埛 uid
璁よ瘉鐘舵€?
璁よ瘉閰嶇疆
澶勭悊閰嶇疆
浠诲姟鐘舵€侊紙stage_task[stage] 涓€琛屼竴 stage锛?
鎶撳彇鐘舵€侊紙fetch_endpoint_state 琛?/ raw_payload 鏄惁瀛樺湪锛?
鎶撳彇杩涘害锛坒etch_progress.cursor锛?
瑙ｆ瀽鐘舵€侊紙stage_task[stage='parsing'].payload.models[*]锛?
瑙ｆ瀽鍥剧墖涓嬭浇鐘舵€侊紙image_asset 琛?+ stage_task.payload.images锛?
澶勭悊鐘舵€侊紙audio_transcription.status锛?
澶勭悊杩涘害锛坰tage_task[stage='processing'].payload.pipelines[*].items锛?
璇锋眰鐘舵€?
闄愭祦鐘舵€侊紙杩涚▼鍐呴┗鐣欙紱涓嶆寔涔呭寲锛?
閲嶈瘯鐘舵€侊紙fetch_endpoint_state.retry_count / RetryDriver 鍐呴儴锛?
澶辫触鐘舵€侊紙stage_error 琛岋級
鎶撳彇缁撴灉锛坮aw_payload锛?
瑙ｆ瀽缁撴灉锛坲ser_profile / video / video_subtitle / article / opus_post / dynamic_event锛?
澶勭悊缁撴灉锛坅udio_transcription锛?
raw 瀛樺偍锛坽uid}.raw.db锛?
temp / asr_cache 瀛樺偍锛坵orkdir 浜岃繘鍒剁洰褰曪級
parsing 瀛樺偍锛坱yped objects 琛?+ workdir/images/锛?
main DB 瀛樺偍锛坽uid}.db锛?
閿欒鐘舵€侊紙stage_error锛?
鎶撳彇鏃堕棿锛坢eta.last_fetched_at_ms / fetched_at_ms锛?
瑙ｆ瀽鏃堕棿锛坢eta.last_parsed_at_ms / parsed_at_ms锛?
澶勭悊鏃堕棿锛坢eta.last_processed_at_ms / processed_at_ms锛?
```

## 8. 杈圭晫

```text
涓嶅鐞嗘姄鍙栫粨鏋滆涔夛紙鍦ㄥ鐞嗛樁娈靛鐞嗭級
涓嶅浐瀹氱敤鎴风浉鍏虫帴鍙ｆ竻鍗?
涓嶇粫杩?endpoint catalog 闅忔剰鎵╁睍鎶撳彇鑳藉姏
涓嶅仛璺?uid 鑱氬悎灏佽
涓嶅仛璺ㄦ簮褰掍竴鍖?/ 娓呮礂
涓嶆帹閫?
涓嶆毚闇?Python query facade锛堟秷璐规柟璧?SQL锛?
audio 涓嶇洿鎺ヨ鍙?raw_payload 涔嬪鐨勫瓧娈?
_env 涓嶅啓鍏?DB
runner 缂栨帓 client / audio
runner 鏍规嵁 stage_task / stage_error 缂栨帓閲嶈瘯
command 涓嶇洿鎺ヨ皟鐢?client / audio
command 涓嶅啓 raw / workdir / DB锛坵rite 璧?store锛?
stage 鍐?store 涔嬮棿浜掍笉鐩存帴璋冪敤锛堝叡浜?UidContext锛岀敱 command 娉ㄥ叆锛?
璇讳晶涓嶆毚闇?DB 鍐呴儴鐢熶骇鐘舵€佽涔夛紙stage_task / fetch_endpoint_state / stage_error 浠?debug锛?
璇讳晶涓嶈鍙?raw DB锛堥櫎闈炴樉寮?re-parse锛?
stage_error 涓嶇紪鎺掗噸璇?
璋冪敤鏂逛笉鐩存帴鍐?main DB / raw DB / workdir
璋冪敤鏂硅 main DB 鏃舵寜 搂10 绋冲畾鎬ф壙璇烘秷璐瑰唴瀹硅〃 / view
workdir/audio temp 澶勭悊瀹屾垚鍚庡垹闄?
```

## 9. 澶栭儴渚濊禆

```text
bilibili-api-python [GitHub](https://github.com/Nemo2011/bilibili-api)
涓婃父寮€鍙戞枃妗?[nemo2011.github.io/bilibili-api](https://nemo2011.github.io/bilibili-api/)
鏈」鐩?endpoint catalog 浣滀负鎶撳彇鑼冨洿鐪熺浉婧?
Credential 璁よ瘉
Bilibili CDN        瑙嗛闊抽娴佷笅杞?
ASR 寮曟搸             璇煶杞枃瀛?
寮傛璋冪敤
鍏抽敭瀛楀弬鏁拌皟鐢?
璇锋眰鍚庣浼樺厛绾? curl_cffi 鈫?aiohttp
412 椋庨櫓         璇锋眰杩囧揩瑙﹀彂锛涚敱闄愭祦鎺у埗澶勭悊
```

## 10. 鐩綍楠ㄦ灦

```text
bili_unit/                # Python 鍖呮牴锛坧yproject 閲?packages = ["bili_unit"]锛?
鈹溾攢鈹€ __init__.py           # 椤跺眰 helper锛歴ession() / db_path() / raw_db_path() / list_uids() / result DTO + 寮傚父
鈹溾攢鈹€ __main__.py           # 缁熶竴 CLI 鍏ュ彛锛沺ython -m bili_unit <subcommand>
鈹溾攢鈹€ _env.py               # BiliSettings (pydantic-settings)锛沚ili_db_dir 鏄敮涓€瀛樺偍鏍?
鈹溾攢鈹€ _retry.py             # 鍏变韩 RetryDriver锛坒etching / processing 閫氱敤锛?
鈹溾攢鈹€ _db/                  # SQLite 鎸佷箙鍖栧眰锛坧aths / connection / context / DDL锛?
鈹溾攢鈹€ command/              # 鍐欎晶鍏ュ彛锛汢iliCommand 鍖呰涓?stage 鐨?command
鈹溾攢鈹€ fetching/             # 鎶撳彇闃舵锛坅uth / _bilibili_adapter / rate_limit / runner/ / _store / command锛?
鈹溾攢鈹€ parsing/              # 瑙ｆ瀽闃舵锛坢odels / _images / specs / materializer / _store / command锛?
鈹溾攢鈹€ processing/           # 澶勭悊闃舵锛坅udio/ / runner/ / _store / command锛?
鈹斺攢鈹€ tests/                # pytest 娴嬭瘯鐩綍
```

杩愯鏃舵暟鎹粯璁よ惤鍦ㄥ伐浣滅洰褰曚笅鐨?`output/bili/...`锛岀敱 `BILI_DB_DIR` 瑕嗙洊锛涗笉鍦?Python 鍖呯洰褰曞唴銆?

```text
浠ｇ爜鐜扮姸锛堢粨鏋?vs 瀹炵幇锛?
  涓婅〃涓?bili_unit 浠撳簱褰撳墠鐨勫疄闄呭竷灞€锛屽寘绾у舰鎬佺ǔ瀹氥€?
  fetching / parsing / processing 浣滀负闃舵瀛愬寘瀛樺湪锛涗笁涓?stage 閫氳繃鍏变韩 UidContext
  鍦ㄥ悓涓€瀵?SQLite DB 涓婂崗浣滐紝bili_unit/command/ 浣滀负璺ㄩ樁娈靛啓渚х粺涓€鍏ュ彛銆?
璋冪敤鏂归€氳繃 sqlite3.connect(bili_unit.db_path(uid)) 鐩存帴娑堣垂涓?DB锛?
  涓嶅啀鏈?bili_unit/query/ 鍖咃紱manifest 鏄?manifest_summary SQL view锛屼笉鏄嫭绔?stage銆?
  raw / workdir / DB 鐨勭墿鐞嗙洰褰曠敱 BILI_DB_DIR 鎺у埗锛岀粨鏋勪笂涓嶅睘浜庝唬鐮佸寘銆?
```

