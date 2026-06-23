# 鏂?Agent 鎺ュ叆鍘熷垯

杩欎唤鏂囨。鏄帴鍏ユ柊 agent 鐨勭煭鐗堟墽琛岃鑼冦€傜洰鏍囨槸璁╂柊 agent 澶嶇敤缁熶竴鐨?`repair + eval` harness锛屽苟淇濇寔鍜?Codex銆乵ini_swe_agent銆丱penHands銆乴ive_swe_agent 涓€鏍风殑鍏钩鎬ц竟鐣屻€?
## 鎬诲師鍒?
1. 浼樺厛璧?agent 瀹樻柟 CLI锛屼笉浼樺厛鐩存帴璋冪敤 Python API銆?2. `repair` 鍜?`eval` 蹇呴』褰诲簳瑙ｈ€︺€?3. `diff agent patch` 灞炰簬 `repair` 鐨勫浐瀹氬悗澶勭悊锛屼笉灞炰簬 `eval`銆?4. agent 蹇呴』鐩存帴淇敼 worktree 涓殑浠撳簱鏂囦欢銆?5. 涓嶅厑璁?agent 杈撳嚭 patch銆乨iff銆乤pply_patch 鏂囨湰鏉ヤ唬鏇跨湡瀹炴枃浠朵慨鏀广€?6. 濡傛灉 worktree 涓嶅彲鍐欙紝agent 蹇呴』鐩存帴缁撴潫鏈 repair锛涘灞?harness 鍙細 diff 鍑虹┖ `.diff`銆?7. 涓ョ浠讳綍鈥滄枃鏈?patch 鍥炴斁鍚庨棬鈥濓細
   - 涓嶅厑璁镐粠 agent 鏈€鍚庝竴鏉℃秷鎭噷鎻愬彇 patch 鍐嶅洖鏀惧埌浠撳簱銆?   - 涓嶅厑璁镐粠 `stdout` / `stderr` 閲屾彁鍙?patch 鍐嶅洖鏀惧埌浠撳簱銆?   - 涓嶅厑璁镐粠鍘熺敓 trajectory 鏂囦欢閲屾彁鍙?patch 鍐嶅洖鏀惧埌浠撳簱銆?8. 閫氱敤瀹炵幇灏介噺鏀捐繘 `src/utils/`锛宎gent 鐩綍閲屽彧鏀捐杽灏佽鑴氭湰鍜屽繀瑕佺殑鏈€灏忓師浠撲唬鐮併€?
## 鐩綍缁撴瀯

鏂?agent 鏀惧湪锛?
```text
src/
  empirical_agents/
    <agent_name>/
      build_worktree/
      run_agent_fixing/
      diff_agent_patch/
```

濡傛灉蹇呴』淇濈暀鍘熶粨鏈€灏忎唬鐮佸瓙闆嗭紝鍙互棰濆鏀撅細

```text
org_src/
```

mini_swe_agent 鍜?live_swe_agent 閮藉睘浜庤繖绉嶆儏鍐点€侺ive-SWE-agent 瀹樻柟璇存槑鍏跺熀浜?mini-swe-agent锛屼粎鐢ㄥ皯閲?config 淇敼锛屽洜姝ゆ湰浠撳簱鐨?live_swe_agent 涔熷鐢?mini_swe_agent 鐨?runner 鍜屾渶灏?`org_src`銆?
Claude Code 涓嶉渶瑕佷繚鐣欏師浠撲唬鐮侊紝鐩存帴閫氳繃瀹樻柟 CLI 鐨勯潪浜や簰妯″紡鎺ュ叆銆?
## repair 涓诲叆鍙?
姣忎釜 agent 搴旀彁渚涗竴涓€诲叆鍙ｏ紝渚嬪锛?
```text
src/empirical_agents/<agent_name>/<agent_name>.py
```

缁熶竴鍙傛暟锛?
```powershell
--jsonl-list path1 path2 path3
--instance-id zulip__zulip-6562
--model MODEL_NAME
--api-key API_KEY
--thinking-effort high
--agent-timeout-seconds 1800
--agent-edit-mode danger-full-access
```

绾﹀畾锛?
- `--instance-id` 鍙€夛紱涓嶄紶鏃舵壒閲忚窇 `--jsonl-list` 閲岀殑鍏ㄩ儴 instance銆?- `--thinking-effort` 榛樿 `high`銆?- `--api-key` 涓嶄紶鏃讹紝淇濇寔璇?agent 鑷韩宸叉湁鐨勯粯璁よ璇佹柟寮忋€?- `<agent_model>` 杈撳嚭閿繀椤荤敱 agent family 鍜屾ā鍨嬪悕娲剧敓锛屼緥濡?`live_swe_agent_gpt54`銆?
## repair 缂栨帓椤哄簭

姣忎釜 agent 鐨勬€诲叆鍙ｅ彧璐熻矗椤哄簭璋冨害杩欎笁姝ワ細

1. `build_worktree`
2. `run_agent_fixing`
3. `diff_agent_patch`

杩欐槸涓€涓畬鏁寸殑 repair 澶ф楠ゃ€俛gent 杩愯缁撴潫鍚庯紝patch 鍙兘鐢卞灞?harness diff worktree 寰楀埌銆?
## eval

eval 涓嶈閲嶅鍐欏湪姣忎釜 agent 鐩綍閲屻€傜粺涓€璧板叡浜叆鍙ｏ細

```text
src/eval/eval.py
```

缁熶竴杈撳叆锛?
- `--jsonl-list`
- `--instance-id`
- `--agent-name`
- 鍗曟潯妯″紡锛歚--agent-patch-path`
- 鎵归噺妯″紡锛歚--agent-patch-root`

## 杈撳嚭钀界洏

缁熶竴钀藉埌锛?
```text
output_data/
  <agent_model>/
    repair_results/
      agent_patch/<repo>/<instance>.diff
      patch_status/<repo>/<instance>.json
    trajectory/<repo>/<instance>/
      prompt.md
      ...agent 鍘熺敓杞ㄨ抗鏂囦欢...
      normalized_traj.jsonl
    logs/<repo>/<instance>/
      step1.log
      step2.log
    eval_results/<repo>/<instance>.json
```

璇存槑锛?
- `agent_patch` 灞炰簬 repair 浜х墿銆?- `eval_results` 鍙斁鏈€缁堣瘎娴嬬粨鏋溿€?- 鍚屼竴 instance 閲嶈窇榛樿鍏ㄨ鐩栥€?
## trajectory

姣忎釜 instance 鐨?trajectory 鐩綍鑷冲皯鍖呭惈锛?
- `prompt.md`
- agent 鍘熺敓杞ㄨ抗鏂囦欢
- `normalized_traj.jsonl`

`normalized_traj.jsonl` 蹇呴』鏈夛細

- `session_start`
- 涓棿鏍囧噯鍖栦簨浠?- `session_end`

## logs

`logs/` 鍙褰曟祦绋嬬姸鎬侊紝涓嶉噸澶嶈褰?agent 鍘熺敓杞ㄨ抗鍐呭锛?
- `step1.log`锛歳epair 鏃ュ織
- `step2.log`锛歟val 鏃ュ織

## 鐘舵€?JSON

repair 渚э細

- `image_available`
- `workspace_prepared`
- `workspace_writable`
- `agent_run_completed`
- `agent_modified_worktree`
- `agent_patch_generated`

eval 渚э細

- `eval_workspace_prepared`
- `agent_patch_applied`
- `trigger_test_patch_applied`
- `regression_test_patch_applied`
- `trigger_test_passed`
- `regression_test_passed`
- `agent_patch_passed`

## 鎺ュ叆妫€鏌ユ竻鍗?
鎺ュ叆鏂?agent 鏃惰嚦灏戠‘璁わ細

- 鏄惁鑳介€氳繃 CLI 椹卞姩銆?- 鏄惁鑳界洿鎺ヤ慨鏀?worktree銆?- worktree 涓嶅彲鍐欐椂鏄惁鐩存帴鍋滄锛岃€屼笉鏄緭鍑?patch銆?- 鏄惁褰诲簳绂佺敤浜嗕换浣曟枃鏈?patch 鍥炴斁 fallback銆?- 鏄惁鑳芥妸 prompt 鍗曠嫭钀芥垚 `prompt.md`銆?- 鏄惁鑳戒繚鐣欏師鐢?trajectory銆?- 鏄惁鑳芥淳鐢熷嚭 `normalized_traj.jsonl`銆?- 鏄惁鑳芥寜缁熶竴鐩綍钀界洏 patch銆乴ogs銆乪val 缁撴灉銆?
