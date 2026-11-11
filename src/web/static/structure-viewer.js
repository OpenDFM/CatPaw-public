let library;
let viewer;
let currentSnapshot;
const styles = {
  ball: {sphere:{scale:0.25,colorscheme:'Jmol'},stick:{radius:0.12,colorscheme:'Jmol'}},
  sphere: {sphere:{colorscheme:'Jmol'}},
  stick: {stick:{radius:0.15,colorscheme:'Jmol'}},
};

export async function prepare() {
  if (!library) library = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://3Dmol.org/build/3Dmol-min.js';
    script.onload = resolve;
    script.onerror = () => { library = null; script.remove(); reject(new Error(t("无法加载三维查看器，请检查网络连接后重试。"))); };
    document.head.append(script);
  });
  await library;
}

export function show(snapshot) {
  currentSnapshot = snapshot;
  const canvas = document.getElementById('preview-canvas');
  if (!viewer) {
    viewer = $3Dmol.createViewer(canvas, {backgroundColor:background(), antialias:true});
    new ResizeObserver(() => viewer.resize()).observe(canvas);
    document.getElementById('preview-style').onchange = event => {
      viewer.setStyle({}, styles[event.target.value]); viewer.render();
    };
    document.getElementById('reset-preview').onclick = () => { viewer.zoomTo(); viewer.render(); };
  }
  viewer.clear();
  const model = viewer.addModel(snapshot.cif, 'cif');
  // Fill periodic faces, edges and corners while keeping a single unit cell.
  viewer.replicateUnitCell(1, 1, 1, model, true);
  viewer.setStyle({}, styles[document.getElementById('preview-style').value]);
  viewer.addUnitCell(model, {
    box:{color:'#8b949e'},
    astyle:{hidden:true}, bstyle:{hidden:true}, cstyle:{hidden:true},
  });
  describeSnapshot();
  viewer.setClickable({}, true, atom => {
    document.getElementById('preview-atom').textContent = `${atom.elem} · x ${atom.x.toFixed(3)} / y ${atom.y.toFixed(3)} / z ${atom.z.toFixed(3)} Å`;
  });
  viewer.zoomTo(); viewer.resize(); viewer.render();
}

function background() { return getComputedStyle(document.documentElement).getPropertyValue('--surface').trim(); }
window.addEventListener('themechange', () => { if (viewer) { viewer.setBackgroundColor(background()); viewer.render(); } });

function describeSnapshot() {
  if (!currentSnapshot) return;
  const snapshot = currentSnapshot;
  document.getElementById('preview-atom').textContent = `${snapshot.formula} · ${snapshot.atoms}${t(" 原子 · ")}${t("拖动旋转，滚轮缩放，点击原子查看坐标")}`;
}
window.addEventListener('languagechange', describeSnapshot);
