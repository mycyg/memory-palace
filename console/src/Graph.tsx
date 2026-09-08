import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
export function Graph({
  data,
  onSelect,
}: {
  data: any;
  onSelect: (id: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [dimension, setDimension] = useState(3);
  useEffect(() => {
    if (!ref.current || !data?.nodes.length) return;
    const root = ref.current,
      scene = new THREE.Scene();
    scene.background = new THREE.Color("#f4f2ed");
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    root.appendChild(renderer.domElement);
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 1000);
    camera.position.set(0, 0, 16);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableRotate = dimension === 3;
    controls.enableDamping = false;
    const geometries: THREE.BufferGeometry[] = [],
      materials: THREE.Material[] = [];
    const nodes: any[] = [];
    const map = new Map<string, THREE.Vector3>();
    for (const node of data.nodes) {
      const p = new THREE.Vector3(
        ...(node.position as [number, number, number]),
      );
      if (dimension === 2) p.z = 0;
      map.set(node.id, p);
      const g = new THREE.SphereGeometry(0.18, 12, 8),
        m = new THREE.MeshBasicMaterial({
          color:
            node.kind === "knowledge"
              ? "#789a90"
              : node.kind === "relationship"
                ? "#c894a7"
                : "#bc765a",
        });
      geometries.push(g);
      materials.push(m);
      const mesh = new THREE.Mesh(g, m);
      mesh.position.copy(p);
      mesh.userData = node;
      scene.add(mesh);
      nodes.push(mesh);
    }
    const bounds = new THREE.Box3().setFromPoints([...map.values()]);
    const center = bounds.getCenter(new THREE.Vector3());
    const radius = Math.max(
      2,
      bounds.getSize(new THREE.Vector3()).length() / 2,
    );
    controls.target.copy(center);
    camera.position
      .copy(center)
      .add(
        new THREE.Vector3(
          0,
          0,
          (radius / Math.tan(THREE.MathUtils.degToRad(25))) * 1.35,
        ),
      );
    controls.update();
    for (const edge of data.edges) {
      const a = map.get(edge.subject),
        b = map.get(edge.object);
      if (a && b) {
        const g = new THREE.BufferGeometry().setFromPoints([a, b]),
          m = new THREE.LineBasicMaterial({
            color: "#c7cfca",
            transparent: true,
            opacity: 0.6,
          });
        geometries.push(g);
        materials.push(m);
        scene.add(new THREE.Line(g, m));
      }
    }
    const draw = () => renderer.render(scene, camera);
    controls.addEventListener("change", draw);
    const resize = new ResizeObserver(() => {
      const w = root.clientWidth,
        h = root.clientHeight;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      draw();
    });
    resize.observe(root);
    let down = { x: 0, y: 0 };
    const pointerdown = (e: PointerEvent) => {
      down = { x: e.clientX, y: e.clientY };
    };
    const select = (e: PointerEvent) => {
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      const r = root.getBoundingClientRect(),
        ray = new THREE.Raycaster();
      ray.setFromCamera(
        new THREE.Vector2(
          ((e.clientX - r.left) / r.width) * 2 - 1,
          (-(e.clientY - r.top) / r.height) * 2 + 1,
        ),
        camera,
      );
      const hit = ray.intersectObjects(nodes)[0];
      if (hit) onSelect(hit.object.userData.id);
    };
    renderer.domElement.addEventListener("pointerdown", pointerdown);
    renderer.domElement.addEventListener("pointerup", select);
    draw();
    return () => {
      resize.disconnect();
      controls.dispose();
      geometries.forEach((x) => x.dispose());
      materials.forEach((x) => x.dispose());
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [data, dimension, onSelect]);
  return (
    <div className="graph-wrap">
      <div className="graph-tools">
        <div className="segmented">
          {[2, 3].map((d) => (
            <button
              key={d}
              className={dimension === d ? "selected" : ""}
              onClick={() => setDimension(d)}
            >
              {d}D
            </button>
          ))}
        </div>
        <span>{data?.nodes.length ?? 0} 个节点 · 拖动旋转，滚动缩放</span>
        <select
          aria-label="按标题读取节点"
          value=""
          onChange={(e) => onSelect(e.target.value)}
        >
          <option value="">读取节点…</option>
          {data?.nodes.map((n: any) => (
            <option key={n.id} value={n.id}>
              {n.title}
            </option>
          ))}
        </select>
      </div>
      <div className="graph" ref={ref} aria-label="主题关系图" />
      {!data?.nodes.length && (
        <div className="graph-empty">
          添加关系或整理主题后，连接会出现在这里。
        </div>
      )}
    </div>
  );
}
