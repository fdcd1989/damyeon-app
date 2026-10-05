/* 평가 화면 마스코트 말풍선.
   원칙: 시간 간격으로 계속 말하지 않고 '상황'에 반응한다 / 점수를 유도하는 말은 하지 않는다 / 글 쓰는 중에는 끼어들지 않는다 /
         ✕ 와 '그만 말하기'로 끌 수 있다 / OS '동작 줄이기'·마감 후에는 말하지 않는다.
   ▼ 문구는 아래 LINES 에서 고치면 됩니다. {next} 는 다음에 평가할 사람 이름(없으면 해당 문구는 건너뜀). */
(function () {
  var LINES = {
    personDone: ['이 분은 다 끝났어요! 다음은 {next}님이에요 👉', '한 분 완료! 다음은 {next}님이에요 👍', '수고했어요! {next}님도 이어서 해볼까요?'],
    halfway:    ['절반 왔어요! 잘하고 있어요 💪', '벌써 반이나 했어요, 조금만 더요!'],
    last:       ['마지막 한 건 남았어요! {next}님만 하면 끝이에요 🏁'],
    lastNoName: ['마지막 한 건 남았어요, 거의 다 왔어요! 🏁'],
    allDone:    ['모든 평가를 마쳤어요! 정말 고생하셨어요 🎉'],
    na:         ['업무 접점이 없으면 N/A가 맞아요, 편하게 표시하세요 🙂', '함께 일해 본 적이 없다면 N/A로 충분해요'],
    idleHelp:   ['점수가 고민되면 N/A로 두거나 나중에 이어서 해도 돼요. 정답은 없어요, 느낀 그대로면 충분해요 🙂'],
    idleDirty:  ['입력한 내용은 [저장]을 누르거나 다른 분으로 이동할 때 자동 저장돼요 💾'],
    unmute:     ['조용히 있을게요 🤫 다시 말하게 할까요?']
  };
  var CFG = { idleMs: 45000, showMs: 5500, idleShowMs: 8000, gapMs: 6000, maxNaPerSession: 2, maxIdlePerSession: 3, maxPersonDonePerSession: 6 };

  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var locked = (typeof IS_LOCKED !== 'undefined') && IS_LOCKED;
  var mascot = document.getElementById('mascot');
  if (!mascot) return;

  function store(kind) {                                   // 저장소가 막힌 환경(시크릿 모드 등)에서도 오류 없이 동작
    var s; try { s = kind === 'local' ? window.localStorage : window.sessionStorage; s.getItem('x'); } catch (e) { s = null; }
    return {
      get: function (k) { try { return s ? s.getItem(k) : null; } catch (e) { return null; } },
      set: function (k, v) { try { if (s) s.setItem(k, v); } catch (e) {} }
    };
  }
  var local = store('local'), sess = store('session');
  var muted = local.get('mascotMute') === '1';
  var count = function (k) { return parseInt(sess.get(k) || '0', 10) || 0; };
  var bump = function (k) { sess.set(k, String(count(k) + 1)); };

  /* ---------- 말풍선 DOM ---------- */
  var box = document.createElement('div');
  box.className = 'mascot-bubble'; box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
  box.innerHTML = '<span class="mb-text"></span><button type="button" class="mb-close" aria-label="말풍선 닫기">✕</button>' +
                  '<button type="button" class="mb-act"></button><span class="mb-tail"></span>';
  document.body.appendChild(box);
  var elText = box.querySelector('.mb-text'), elClose = box.querySelector('.mb-close'), elAct = box.querySelector('.mb-act');
  var hideTimer = null, visible = false, lastEnd = 0, lastInput = 0;

  function pick(list) { return list[Math.floor(Math.random() * list.length)]; }
  function mode() { var st = window.Mascot && window.Mascot.state && window.Mascot.state(); return st === 'home' ? 'left' : 'below'; }

  function place() {
    if (!visible) return;
    var r = mascot.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
    if (r.bottom < 0 || r.top > vh) { hide(); return; }    // 캐릭터가 화면 밖이면 말풍선도 접는다
    var m = mode(), W = Math.min(300, vw - 16);
    box.style.maxWidth = W + 'px'; box.style.left = '0px'; box.style.top = '0px';
    var bw = box.offsetWidth, bh = box.offsetHeight, left, top;
    box.classList.toggle('is-left', m === 'left'); box.classList.toggle('is-below', m !== 'left');
    if (m === 'left') {                                    // 캐릭터 왼쪽, 세로 가운데 (제목 줄의 N/A 안내 옆 빈 곳)
      left = r.left - 12 - bw; top = r.top + r.height / 2 - bh / 2;
      box.style.setProperty('--tail', Math.max(10, Math.min(bh - 22, r.top + r.height / 2 - top - 6)) + 'px');
    } else {                                               // 캐릭터 아래, 오른쪽 정렬 (사이드바/모바일의 진행 현황 줄 위)
      left = r.right - bw; top = r.bottom + 10;
      box.style.setProperty('--tail', Math.max(14, Math.min(bw - 28, r.left + r.width / 2 - Math.max(8, left) - 6)) + 'px');
    }
    left = Math.max(8, Math.min(left, vw - bw - 8)); top = Math.max(8, Math.min(top, vh - bh - 8));
    box.style.left = left + 'px'; box.style.top = top + 'px';
  }
  function hide() { visible = false; clearTimeout(hideTimer); box.classList.remove('show'); lastEnd = Date.now(); }
  function show(text, ms, action) {
    clearTimeout(hideTimer);
    elText.textContent = text;
    if (action) { elAct.textContent = action.label; elAct.onclick = action.run; elAct.style.display = ''; }
    else { elAct.style.display = 'none'; elAct.onclick = null; }
    visible = true; place(); void box.offsetWidth; box.classList.add('show');
    hideTimer = setTimeout(hide, ms || CFG.showMs);
  }
  /* 끄기/켜기: 말풍선의 '그만 말하기', 캐릭터 클릭 후 '다시 말하기', 사이드바 하단의 토글 버튼이 모두 같은 함수를 쓴다 */
  var toggle = document.getElementById('bubble-toggle');
  function refreshToggle() {
    if (!toggle) return;
    toggle.hidden = !!(reduce || locked);                    // 동작 줄이기·마감 후에는 말풍선 자체가 없으므로 버튼도 숨김
    toggle.setAttribute('aria-pressed', muted ? 'false' : 'true');
    toggle.textContent = muted ? '💬 캐릭터 말풍선: 꺼짐 (눌러서 켜기)' : '💬 캐릭터 말풍선: 켜짐 (눌러서 끄기)';
  }
  function setMuted(v, announce) {
    muted = !!v; local.set('mascotMute', muted ? '1' : '0'); hide(); refreshToggle();
    if (!muted && announce && !reduce && !locked) show('다시 말할게요! 😊', 3000, muteAction);
  }
  var muteAction = { label: '그만 말하기', run: function () { setMuted(true); } };
  if (toggle) toggle.addEventListener('click', function () { setMuted(!muted, true); });
  refreshToggle();

  elClose.addEventListener('click', hide);
  document.addEventListener('mascot:moved', function (e) { if (e.detail && e.detail.moving) hide(); else place(); });
  var ticking = false;
  var reposition = function () { if (!visible || ticking) return; ticking = true; requestAnimationFrame(function () { ticking = false; place(); }); };
  window.addEventListener('scroll', reposition, { passive: true });
  window.addEventListener('resize', reposition);

  /* ---------- 말할 수 있는 상황인지 ---------- */
  function typing() {
    var a = document.activeElement;
    return a && (a.tagName === 'TEXTAREA' || (a.tagName === 'INPUT' && a.type === 'text')) && Date.now() - lastInput < 8000;
  }
  function canSpeak(opts) {
    if (muted || reduce || locked || document.hidden) return false;
    if (typing()) return false;
    if (!(opts && opts.force) && Date.now() - lastEnd < CFG.gapMs && !visible) return false;
    return true;
  }
  function say(kind, vars, ms) {
    var list = LINES[kind]; if (!list) return false;
    var text = pick(list);
    if (text.indexOf('{next}') >= 0) { if (!vars || !vars.next) return false; text = text.split('{next}').join(vars.next); }
    show(text, ms, muteAction); return true;
  }

  /* ---------- 저장 후: 진행 상황에 맞는 한마디 ---------- */
  function nextName() {
    var items = Array.prototype.slice.call(document.querySelectorAll('.eval-item'));
    var idx = items.findIndex(function (n) { return n.classList.contains('is-current'); });
    var ok = function (n) { return !n.classList.contains('is-done') && !n.classList.contains('is-current'); };
    var cand = items.slice(idx + 1).filter(ok)[0] || items.slice(0, Math.max(idx, 0)).filter(ok)[0];
    var nm = cand && cand.querySelector('.name'); return nm ? nm.textContent.trim() : null;
  }
  var prevDone = (function () { var m = (document.getElementById('progress-count') || {}).textContent; var n = m && m.match(/(\d+)\s*\/\s*(\d+)/); return n ? +n[1] : 0; })();
  var prevComplete = (function () { var d = document.querySelector('.eval-item.is-current .dot-icon'); return !!d && d.textContent.trim() === '✓'; })();

  function afterSave(data) {
    if (!data) return;
    var remaining = data.total_count - data.done_count, prevRemaining = data.total_count - prevDone;
    var justCompleted = data.complete && !prevComplete;
    var crossedHalf = data.done_count * 2 >= data.total_count && prevDone * 2 < data.total_count && data.total_count >= 4;
    var crossedLast = remaining === 1 && prevRemaining > 1;
    prevDone = data.done_count; prevComplete = !!data.complete;
    if (!canSpeak({ force: true })) return;
    var next = nextName(), spoke = false;
    setTimeout(function () {                                // 저장 알림(위쪽)과 겹치지 않게 약간 뒤에, 캐릭터 반응과 함께
      if (!canSpeak({ force: true })) return;
      if (remaining === 0 && justCompleted) spoke = say('allDone', null, 7000);
      else if (justCompleted && crossedLast) spoke = say(next ? 'last' : 'lastNoName', { next: next });
      else if (justCompleted && crossedHalf) spoke = say('halfway');
      else if (justCompleted && count('mbPersonDone') < CFG.maxPersonDonePerSession && next) { spoke = say('personDone', { next: next }); if (spoke) bump('mbPersonDone'); }
    }, 900);
  }

  /* ---------- N/A ---------- */
  function onNA() {
    if (count('mbNa') >= CFG.maxNaPerSession || !canSpeak()) return;
    if (say('na')) bump('mbNa');
  }

  /* ---------- 오래 멈춰 있을 때: 한 화면에 최대 한 번, 세션 최대 3번 ---------- */
  var idleTimer = null, idleDone = false;
  function armIdle() {
    clearTimeout(idleTimer);
    if (idleDone || muted || reduce || locked) return;
    idleTimer = setTimeout(function () {
      if (idleDone) return;
      if (count('mbIdle') >= CFG.maxIdlePerSession) { idleDone = true; return; }
      if (!canSpeak()) { armIdle(); return; }              // 글 쓰는 중이면 조금 더 기다린다
      idleDone = true; bump('mbIdle');
      say((typeof dirty !== 'undefined' && dirty) ? 'idleDirty' : 'idleHelp', null, CFG.idleShowMs);
    }, CFG.idleMs);
  }
  ['pointerdown', 'keydown', 'touchstart', 'change'].forEach(function (ev) { document.addEventListener(ev, armIdle, true); });
  window.addEventListener('scroll', armIdle, { passive: true });
  document.addEventListener('input', function () { lastInput = Date.now(); armIdle(); }, true);
  document.addEventListener('visibilitychange', function () { if (document.hidden) { hide(); } else { armIdle(); } });
  armIdle();

  /* ---------- 캐릭터를 눌렀는데 조용히 설정이라면 다시 켤 수 있게 ---------- */
  mascot.addEventListener('click', function () {
    if (!muted || reduce) return;
    show(pick(LINES.unmute), 7000, { label: '다시 말하기', run: function () { setMuted(false, true); } });
  });

  window.MascotBubble = { afterSave: afterSave, onNA: onNA, say: function (k, v) { return canSpeak({ force: true }) && say(k, v); },
                          isMuted: function () { return muted; }, setMuted: setMuted };
})();
