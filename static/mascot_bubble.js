/* 평가 화면 마스코트 말풍선.
   - 화면이 열리면 5~6초마다 카테고리를 돌아가며 도움말을 한 줄씩 말한다. 캐릭터를 누르면 즉시 다음 말을 하고 거기서부터 이어진다.
   - 글 쓰는 중·OFF·동작 줄이기·마감 후에는 말하지 않는다. 캐릭터가 사이드바로 내려간 동안(스크롤)은 자동 말하기를 쉰다.
   - 점수를 유도하는 말은 하지 않는다. 문구는 모두 이 앱에 실제로 있는 기능만 설명한다.
   ▼ 문구는 CATS / INTRO / SITUATION 에서, 주기와 횟수는 CFG 에서 고치면 됩니다. */
(function () {
  var CFG = {
    auto: !window.__MB_NO_AUTO__,        // (테스트용 스위치: window.__MB_NO_AUTO__ = true 이면 자동 말하기 끔)
    autoStates: ['home'],                // 캐릭터가 제목 줄에 있을 때만 자동으로 말함 ('dock','mobile' 을 추가하면 그때도 말함)
    firstDelayMs: 1200, periodMin: 5000, periodMax: 6000, gapMs: 600,   // 말풍선은 (주기 - gapMs) 동안 보인다
    pauseAfterCloseMs: 60000,            // ✕ 로 닫으면 이만큼 자동 말하기를 쉼
    ctxCooldownMs: 90000,                // 상황 맞춤 문구(저장 안내 등)를 다시 말하기까지의 간격
    introMs: 8000
  };

  /* ---------- 문구 ---------- */
  var INTRO = '처음이시죠? 느낀 그대로 골라 주세요 🙂 다른 분으로 넘어가면 자동 저장돼요. 말풍선은 아래 스위치로 끌 수 있어요.';
  // 카테고리별 로테이션. 한 카테고리 안에서는 한 바퀴를 다 돌 때까지 같은 문구가 반복되지 않는다.
  var CATS = [
    { id: 'A', name: '저장·이어하기', lines: [
      "넘어가면 자동 저장돼요. 하단에 '✓ 저장됨'이 보이면 끝! 💾",
      '다 못 끝내도 괜찮아요. 저장한 내용은 그대로, 이어서 하면 돼요 ☕',
      "목록의 '이어서 평가하기'가 다음 순서를 알려줘요 👉",
      '마감 전에는 언제든 고칠 수 있어요. 마감일은 목록에서 확인! 🗓',
      '화면이 이상하면 새로고침해 보세요. 저장한 내용은 남아 있어요.'] },
    { id: 'B', name: '점수 고르기', lines: [
      { t: function () { return scaleEven() ? '정답은 없어요. 중간값이 없으니 더 가까운 쪽을 골라 보세요 🙂' : '정답은 없어요. 느낀 그대로 가까운 쪽을 골라 보세요 🙂'; } },
      '최근 함께 일한 장면을 떠올리면 고르기 쉬워요.',
      '문항마다 따로 판단해요. 한 인상이 번지지 않게요.'] },
    { id: 'C', name: 'N/A', lines: [
      '함께 일한 적이 거의 없다면 N/A가 맞아요. 억지로 안 줘도 돼요.',
      "전부 해당 없으면 '전체 N/A'를! N/A도 응답이라 완료로 쳐요 ✅"] },
    { id: 'D', name: '코멘트·총평', lines: [
      "코멘트·총평은 선택이에요. 적고 싶을 때만 '+ 코멘트 추가'!",
      '구체적 상황·행동 중심으로 적으면 성장에 큰 도움이 돼요 🌱',
      '점수를 안 고르면 코멘트가 저장 안 돼요. N/A도 선택이에요 ⚠'] },
    { id: 'E', name: '진행·응원', lines: [
      '왼쪽 목록: 초록 체크=완료, 주황 …=작성 중이에요 ✅',
      { t: function () { return '지금 ' + remaining() + '건 남았어요. 천천히 해도 괜찮아요 🙂'; }, ok: function () { return remaining() > 0; } },
      '한 분 한 분 소중한 의견이에요. 끝까지 응원할게요 💚',
      '눌러 줘서 고마워요! 궁금하면 또 눌러 보세요 😄'] }
  ];
  // 상황 문구(저장 직후 진행 상황에 맞춰 한 번씩). {next} = 다음에 평가할 사람 이름
  var SITUATION = {
    personDone: ['이 분은 다 끝났어요! 다음은 {next}님이에요 👉', '한 분 완료! 다음은 {next}님이에요 👍', '수고했어요! {next}님도 이어서 해볼까요?'],
    halfway:    ['절반 왔어요! 잘하고 있어요 💪', '벌써 반이나 했어요, 조금만 더요!'],
    last:       ['마지막 한 건 남았어요! {next}님만 하면 끝이에요 🏁'],
    lastNoName: ['마지막 한 건 남았어요, 거의 다 왔어요! 🏁'],
    allDone:    ['모든 평가를 마쳤어요! 정말 고생하셨어요 🎉'],
    onSwitchOn: ['다시 말할게요! 😊']
  };

  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var locked = (typeof IS_LOCKED !== 'undefined') && IS_LOCKED;
  var mascot = document.getElementById('mascot');
  if (!mascot) return;

  /* ---------- 저장소(막힌 환경에서도 오류 없이) ---------- */
  function store(kind) {
    var s; try { s = kind === 'local' ? window.localStorage : window.sessionStorage; s.getItem('x'); } catch (e) { s = null; }
    return { get: function (k) { try { return s ? s.getItem(k) : null; } catch (e) { return null; } },
             set: function (k, v) { try { if (s) s.setItem(k, v); } catch (e) {} } };
  }
  var local = store('local'), sess = store('session');
  var muted = local.get('mascotMute') === '1';

  /* ---------- 화면에서 읽는 값 ---------- */
  function progress() { var t = (document.getElementById('progress-count') || {}).textContent, m = t && t.match(/(\d+)\s*\/\s*(\d+)/); return m ? { done: +m[1], total: +m[2] } : { done: 0, total: 0 }; }
  function remaining() { var p = progress(); return Math.max(p.total - p.done, 0); }
  function scaleEven() { var n = document.querySelectorAll('input[name="score_0"]:not([value="NA"])').length; return n > 0 && n % 2 === 0; }
  function dirtyNow() { return (typeof dirty !== 'undefined') && !!dirty; }
  function commentWithoutScore() {
    var tas = document.querySelectorAll('input[name^="comment_"], textarea[name^="comment_"]');   // 문항별 코멘트 입력칸
    for (var i = 0; i < tas.length; i++) {
      var idx = tas[i].name.replace('comment_', '');
      if (tas[i].value.trim() && !document.querySelector('input[name="score_' + idx + '"]:checked')) return true;
    }
    return false;
  }
  function allNA() {
    var names = {}; document.querySelectorAll('input[type=radio][name^="score_"]').forEach(function (r) { names[r.name] = true; });
    var keys = Object.keys(names); if (!keys.length) return false;
    return keys.every(function (n) { var c = document.querySelector('input[name="' + n + '"]:checked'); return c && c.value === 'NA'; });
  }

  /* ---------- 말풍선 DOM ---------- */
  var box = document.createElement('div');
  box.className = 'mascot-bubble'; box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'off');
  box.innerHTML = '<span class="mb-text"></span><button type="button" class="mb-close" aria-label="말풍선 닫기">✕</button><span class="mb-tail"></span>';
  document.body.appendChild(box);
  var elText = box.querySelector('.mb-text'), elClose = box.querySelector('.mb-close');
  var hideTimer = null, visible = false, lastInput = 0, moving = false, pausedUntil = 0;

  function pick(list) { return list[Math.floor(Math.random() * list.length)]; }
  function state() { return window.Mascot && window.Mascot.state && window.Mascot.state(); }
  function mode() { return state() === 'home' ? 'left' : 'below'; }

  function place() {
    if (!visible) return;
    var r = mascot.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
    if (r.bottom < 0 || r.top > vh) { hide(); return; }    // 캐릭터가 화면 밖이면 말풍선도 접는다
    var m = mode(), W = Math.min(300, vw - 16);
    box.style.maxWidth = W + 'px'; box.style.left = '0px'; box.style.top = '0px';
    var bw = box.offsetWidth, bh = box.offsetHeight, left, top;
    box.classList.toggle('is-left', m === 'left'); box.classList.toggle('is-below', m !== 'left');
    if (m === 'left') {                                    // 제목 아래의 빈 공간(캐릭터 왼쪽): 제목·소속 줄과 N/A 카드를 가리지 않는다
      var meta = document.querySelector('.eval-target-meta'), head = document.querySelector('.eval-head');
      var minTop = meta ? meta.getBoundingClientRect().bottom + 6 : r.top;
      var maxTop = head ? head.getBoundingClientRect().bottom - bh - 2 : r.bottom - bh;
      left = r.left - 12 - bw; top = r.top + r.height / 2 - bh / 2;
      top = Math.max(top, minTop); if (maxTop >= minTop) top = Math.min(top, maxTop);
      box.style.setProperty('--tail', Math.max(10, Math.min(bh - 22, r.top + r.height / 2 - top - 6)) + 'px');
    } else {                                               // 사이드바/모바일: 캐릭터와 스위치 아래, 오른쪽 정렬
      var vis = switches.filter(function (x) { return x.offsetParent !== null; })[0];
      left = r.right - bw; top = Math.max(r.bottom, vis ? vis.getBoundingClientRect().bottom : 0) + 8;
      box.style.setProperty('--tail', Math.max(14, Math.min(bw - 28, r.left + r.width / 2 - Math.max(8, left) - 6)) + 'px');
    }
    left = Math.max(8, Math.min(left, vw - bw - 8)); top = Math.max(8, Math.min(top, vh - bh - 8));
    box.style.left = left + 'px'; box.style.top = top + 'px';
  }
  function hide() { visible = false; clearTimeout(hideTimer); box.classList.remove('show'); }
  function show(text, ms, live) {
    clearTimeout(hideTimer);
    box.setAttribute('aria-live', live || 'off');
    elText.textContent = text;
    visible = true; place(); void box.offsetWidth; box.classList.add('show');
    hideTimer = setTimeout(hide, ms);
  }

  /* ---------- 끄기/켜기 스위치 ---------- */
  var switches = Array.prototype.slice.call(document.querySelectorAll('.bubble-switch'));
  function refreshSwitches() {
    switches.forEach(function (sw) {
      sw.hidden = !!(reduce || locked);                      // 동작 줄이기·마감 후에는 말풍선 자체가 없으므로 스위치도 숨김
      sw.setAttribute('aria-checked', muted ? 'false' : 'true');
      var st = sw.querySelector('.bs-state'); if (st) st.textContent = muted ? 'OFF' : 'ON';
    });
  }
  function setMuted(v, announce) {
    muted = !!v; local.set('mascotMute', muted ? '1' : '0'); hide(); refreshSwitches();
    if (muted) { clearTimeout(autoTimer); }
    else { if (announce && !reduce && !locked) show(pick(SITUATION.onSwitchOn), 3000, 'polite'); scheduleAuto(announce ? 3600 + periodMs() : CFG.firstDelayMs); }
  }
  switches.forEach(function (sw) { sw.addEventListener('click', function () { setMuted(!muted, true); }); });
  function syncState(st) { if (st) document.body.setAttribute('data-mascot-state', st); }
  syncState(state());
  refreshSwitches();

  elClose.addEventListener('click', function () { hide(); pausedUntil = Date.now() + CFG.pauseAfterCloseMs; });   // ✕ : 한동안 자동 말하기도 쉼
  document.addEventListener('mascot:moved', function (e) { syncState(e.detail && e.detail.state); moving = !!(e.detail && e.detail.moving); if (moving) hide(); else place(); });
  var ticking = false;
  var reposition = function () { if (!visible || ticking) return; ticking = true; requestAnimationFrame(function () { ticking = false; place(); }); };
  window.addEventListener('scroll', reposition, { passive: true });
  window.addEventListener('resize', reposition);

  /* ---------- 로테이션: 카테고리 순서는 한 바퀴마다 섞고, 바로 직전 카테고리는 피한다. 진행 상태는 브라우저에 기억 ---------- */
  var rot = (function () { try { var o = JSON.parse(local.get('mbRot') || 'null'); if (o && o.order) return o; } catch (e) {} return { order: [], pos: 0, last: null, used: {}, lastLine: {} }; })();
  function saveRot() { local.set('mbRot', JSON.stringify(rot)); }
  function shuffle(a) { for (var i = a.length - 1; i > 0; i--) { var j = Math.floor(Math.random() * (i + 1)); var t = a[i]; a[i] = a[j]; a[j] = t; } return a; }
  function catById(id) { return CATS.filter(function (c) { return c.id === id; })[0]; }
  function lineOk(l) { return typeof l === 'string' || !l.ok || l.ok(); }
  function lineText(l) { return typeof l === 'string' ? l : l.t(); }
  function eligible(cat) { var r = []; cat.lines.forEach(function (l, i) { if (lineOk(l)) r.push(i); }); return r; }

  function nextCategoryId() {
    if (rot.pos >= rot.order.length) {                     // 새 한 바퀴
      var ids = shuffle(CATS.map(function (c) { return c.id; }));
      if (ids.length > 1 && ids[0] === rot.last) { var k = 1 + Math.floor(Math.random() * (ids.length - 1)); var t = ids[0]; ids[0] = ids[k]; ids[k] = t; }
      rot.order = ids; rot.pos = 0;
    }
    return rot.order[rot.pos++];
  }
  function useLine(catId, idx) {                           // 문구를 '말한 것'으로 기록 (카테고리 한 바퀴 안에서 반복 방지)
    var used = rot.used[catId] || []; if (used.indexOf(idx) < 0) used.push(idx);
    rot.used[catId] = used; rot.lastLine[catId] = idx; rot.last = catId; saveRot();
  }
  function lineFrom(catId) {
    var cat = catById(catId), ok = eligible(cat); if (!ok.length) return null;
    var used = rot.used[catId] || [];
    var avail = ok.filter(function (i) { return used.indexOf(i) < 0; });
    if (!avail.length) {                                   // 한 바퀴 끝: 처음부터 다시, 단 방금 한 문구는 바로 반복하지 않음
      rot.used[catId] = []; avail = ok.slice();
      if (avail.length > 1) avail = avail.filter(function (i) { return i !== rot.lastLine[catId]; });
    }
    var idx = pick(avail); useLine(catId, idx);
    return lineText(catById(catId).lines[idx]);
  }
  function rotationPick() { var text = null, guard = 0; while (!text && guard++ < CATS.length) text = lineFrom(nextCategoryId()); return text; }

  /* 지금 상황에 꼭 맞는 문구가 있으면 순서보다 먼저 */
  var ctxAt = {};
  function ctxReady(key) { return Date.now() - (ctxAt[key] || 0) > CFG.ctxCooldownMs; }
  function ctxUse(key) { ctxAt[key] = Date.now(); }
  function contextPick() {
    if (commentWithoutScore() && ctxReady('D3')) { ctxUse('D3'); useLine('D', 2); return lineText(catById('D').lines[2]); }
    if (dirtyNow() && ctxReady('A1')) { ctxUse('A1'); useLine('A', 0); return lineText(catById('A').lines[0]); }
    if (allNA() && ctxReady('C')) { ctxUse('C'); return lineFrom('C'); }
    return null;
  }

  /* ---------- 말하기 ---------- */
  function periodMs() { return CFG.periodMin + Math.random() * (CFG.periodMax - CFG.periodMin); }
  function typing() {
    var a = document.activeElement;
    return !!a && (a.tagName === 'TEXTAREA' || (a.tagName === 'INPUT' && a.type === 'text')) && Date.now() - lastInput < 8000;
  }
  function mascotInView() { var r = mascot.getBoundingClientRect(); return r.bottom > 0 && r.top < window.innerHeight; }
  function canSpeak() { return !muted && !reduce && !locked && !document.hidden; }

  var autoTimer = null, introDone = local.get('mbIntroSeen') === '1';
  function speak(text, live, ms) { show(text, ms || Math.max(3500, periodMs() - CFG.gapMs), live); }
  function speakNext(fromClick) {
    if (!introDone) { introDone = true; local.set('mbIntroSeen', '1'); show(INTRO, CFG.introMs, fromClick ? 'polite' : 'off'); return; }
    var text = contextPick() || rotationPick();
    if (text) speak(text, fromClick ? 'polite' : 'off');
  }
  function scheduleAuto(delay) { clearTimeout(autoTimer); if (!CFG.auto || muted || reduce || locked) return; autoTimer = setTimeout(autoTick, delay); }
  function autoTick() {
    if (!CFG.auto || !canSpeak()) { return scheduleAuto(2000); }
    var waitMore = moving || typing() || !mascotInView() || CFG.autoStates.indexOf(state()) < 0 || Date.now() < pausedUntil;
    if (waitMore) { return scheduleAuto(1500); }           // 말할 수 없는 상황이면 문구를 소비하지 않고 잠시 뒤 다시 확인
    speakNext(false);
    scheduleAuto(periodMs());
  }
  document.addEventListener('input', function () { lastInput = Date.now(); }, true);
  document.addEventListener('visibilitychange', function () { if (document.hidden) hide(); });

  /* 캐릭터 클릭: 지금 바로 다음 말 → 거기서부터 주기가 이어진다 */
  var lastClick = 0;
  mascot.addEventListener('click', function () {
    if (!canSpeak() || Date.now() - lastClick < 300) return;
    lastClick = Date.now(); pausedUntil = 0;
    speakNext(true); scheduleAuto(periodMs());
  });

  /* ---------- 저장 후 진행 상황에 맞는 한마디 (순서와 별개로 먼저 말함) ---------- */
  function nextName() {
    var items = Array.prototype.slice.call(document.querySelectorAll('.eval-item'));
    var idx = items.findIndex(function (n) { return n.classList.contains('is-current'); });
    var ok = function (n) { return !n.classList.contains('is-done') && !n.classList.contains('is-current'); };
    var cand = items.slice(idx + 1).filter(ok)[0] || items.slice(0, Math.max(idx, 0)).filter(ok)[0];
    var nm = cand && cand.querySelector('.name'); return nm ? nm.textContent.trim() : null;
  }
  var prevDone = progress().done;
  var prevComplete = (function () { var d = document.querySelector('.eval-item.is-current .dot-icon'); return !!d && d.textContent.trim() === '✓'; })();
  function situation(kind, vars, ms) {
    var text = pick(SITUATION[kind]);
    if (text.indexOf('{next}') >= 0) { if (!vars || !vars.next) return false; text = text.split('{next}').join(vars.next); }
    speak(text, 'polite', ms); scheduleAuto(periodMs() + 1500); return true;
  }
  function afterSave(data) {
    if (!data) return;
    var remainingNow = data.total_count - data.done_count, prevRemaining = data.total_count - prevDone;
    var justCompleted = data.complete && !prevComplete;
    var crossedHalf = data.done_count * 2 >= data.total_count && prevDone * 2 < data.total_count && data.total_count >= 4;
    var crossedLast = remainingNow === 1 && prevRemaining > 1;
    prevDone = data.done_count; prevComplete = !!data.complete;
    if (!justCompleted || !canSpeak()) return;
    var next = nextName();
    setTimeout(function () {                                // 저장 알림(위쪽)과 겹치지 않게 약간 뒤에, 캐릭터 반응과 함께
      if (!canSpeak() || typing()) return;                  // 그 사이 코멘트를 쓰기 시작했다면 끼어들지 않는다
      if (remainingNow === 0) situation('allDone', null, 7000);
      else if (crossedLast) situation(next ? 'last' : 'lastNoName', { next: next });
      else if (crossedHalf) situation('halfway');
      else if (next) situation('personDone', { next: next });
    }, 900);
  }

  /* 전체 N/A 를 눌렀을 때는 N/A 안내를 바로 */
  var lastNaAt = 0;
  function onNA() {
    if (!canSpeak() || typing() || Date.now() - lastNaAt < 20000) return;
    lastNaAt = Date.now(); ctxUse('C');
    var text = lineFrom('C'); if (text) { speak(text, 'polite'); scheduleAuto(periodMs() + 1000); }
  }

  /* 자동 말하기 시작 */
  var startTimer = setInterval(function () { if (mascot.classList.contains('is-placed')) { clearInterval(startTimer); scheduleAuto(CFG.firstDelayMs); } }, 150);

  window.MascotBubble = {
    afterSave: afterSave, onNA: onNA, isMuted: function () { return muted; }, setMuted: setMuted,
    say: function (k, v) { return canSpeak() && SITUATION[k] ? situation(k, v) : false; },
    restart: function () { scheduleAuto(300); },
    config: CFG,
    debug: { cats: CATS, intro: INTRO, textOf: lineText, rot: function () { return JSON.parse(JSON.stringify(rot)); } }
  };
})();
