/* 마스코트: 페이지가 열리면 한 번 인사(greet), 저장에 성공하면 반응(cheer), 클릭하면 다시 인사.
   - 영상 하나를 구간(초)으로 나눠 재생한다. 소리 없음(muted).
   - 'OS 동작 줄이기'를 켠 사용자, 영상 로딩/재생 실패, 탭이 숨겨진 경우에는 정지 그림만 보여준다. */
(function () {
  var el = document.getElementById('mascot');
  if (!el) return;
  var video = el.querySelector('video');
  var poster = el.querySelector('img');
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var SEG = { greet: [0.0, 4.0], cheer: [7.2, 9.8] };
  var REST = '/static/mascot/rest.webp', HAPPY = '/static/mascot/happy.webp';
  var ready = false, token = 0, rafId = null, restTimer = null;

  function setPlaying(on) { el.classList.toggle('is-playing', on); }
  function stop() {
    token++;
    if (rafId) cancelAnimationFrame(rafId);
    clearTimeout(restTimer);
    try { video.pause(); } catch (e) {}
    setPlaying(false);
  }
  function still(name) {            // 영상을 쓸 수 없을 때: 정지 그림으로만 반응
    if (name !== 'cheer') return;
    poster.src = HAPPY;
    clearTimeout(restTimer);
    restTimer = setTimeout(function () { poster.src = REST; }, 2500);
  }
  function play(name) {
    stop();
    if (reduce || !ready) { still(name); return; }
    var seg = SEG[name], my = token;
    try { video.currentTime = seg[0]; } catch (e) { still(name); return; }
    var p = video.play();
    var begin = function () {
      if (my !== token) return;
      setPlaying(true);
      (function tick() {
        if (my !== token) return;
        if (video.currentTime >= seg[1] || video.ended) {
          video.pause();
          restTimer = setTimeout(function () { if (my === token) setPlaying(false); }, name === 'cheer' ? 700 : 150);
          return;
        }
        rafId = requestAnimationFrame(tick);
      })();
    };
    if (p && p.then) p.then(begin).catch(function () { setPlaying(false); still(name); });
    else begin();
  }

  var started = false;
  function onReady() {
    if (ready) return;
    ready = true;
    if (!started) { started = true; play('greet'); }
  }
  video.addEventListener('canplay', onReady);
  video.addEventListener('error', function () { ready = false; setPlaying(false); });
  if (video.readyState >= 3) onReady();
  setTimeout(function () { if (!started) { started = true; /* 로딩이 늦으면 정지 그림만 유지 */ } }, 4000);

  el.addEventListener('click', function () { play('greet'); });
  document.addEventListener('visibilitychange', function () { if (document.hidden) stop(); });

  window.Mascot = { greet: function () { play('greet'); }, cheer: function () { play('cheer'); } };
})();
