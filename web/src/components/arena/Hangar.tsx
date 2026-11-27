"use client";

import { useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { ContactShadows, Environment, Lightformer, PresentationControls, Sparkles } from "@react-three/drei";
import { ACESFilmicToneMapping, SRGBColorSpace } from "three";
import type { Group } from "three";
import { Anchor, LabelLayer, type AnchorMap } from "./Labels";
import { Robot, type Mood } from "./Robot";

export interface HangarItem {
  id: string;
  team: string;
  agent: string;
  status: string;
  tone: "amber" | "blue" | "green" | "red" | "grey";
  mood: Mood;
}

const TONE = { amber: "#fbbf24", blue: "#7dd3fc", green: "#4ade80", red: "#f87171", grey: "#93a3b8" } as const;

function Pad({ item, x, onOpen, motion, index, anchors }: { item: HangarItem; x: number; onOpen: () => void; motion: boolean; index: number; anchors: AnchorMap }) {
  const lift = useRef<Group>(null);
  const [hover, setHover] = useState(false);
  const color = TONE[item.tone];
  useFrame(() => {
    if (!lift.current) return;
    const y = hover ? 0.15 : 0;
    lift.current.position.y += (y - lift.current.position.y) * 0.15;
  });
  return (
    <group
      position={[x, 0, 0]}
      onPointerOver={(e) => {
        e.stopPropagation();
        setHover(true);
        document.body.style.cursor = "pointer";
      }}
      onPointerOut={() => {
        setHover(false);
        document.body.style.cursor = "";
      }}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      <mesh position={[0, 0.08, 0]} receiveShadow castShadow>
        <cylinderGeometry args={[0.95, 1.02, 0.16, 64]} />
        <meshPhysicalMaterial color="#e8eef6" roughness={0.4} clearcoat={0.5} />
      </mesh>
      <mesh position={[0, 0.165, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[0.8, 0.9, 64]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={2} toneMapped={false} />
      </mesh>
      <group ref={lift} position={[0, 0.17, 0]}>
        <Robot team={item.team} mood={item.mood} scale={1.25} motion={motion} seed={index * 2} />
      </group>
      <Anchor id={item.id} anchors={anchors} position={[0, 2.75, 0]} />
    </group>
  );
}

/** The inbox as a hangar: each agent with an open incident stands on its own pad. */
export default function Hangar({ items, onOpen, motion }: { items: HangarItem[]; onOpen: (id: string) => void; motion: boolean }) {
  const anchors = useMemo<AnchorMap>(() => new Map(), []);
  const gap = 2.6;
  const start = -((items.length - 1) * gap) / 2;
  return (
    <div className="arena">
      <Canvas
        shadows
        dpr={[1, 2]}
        camera={{ position: [0, 2.6, 9], fov: 36 }}
        gl={{ antialias: true, toneMapping: ACESFilmicToneMapping, outputColorSpace: SRGBColorSpace }}
      >
        <color attach="background" args={["#0e1a2d"]} />
        <fog attach="fog" args={["#0e1a2d", 12, 26]} />
        <ambientLight intensity={0.55} />
        <hemisphereLight args={["#d6e8ff", "#0b1424", 0.7]} />
        <directionalLight position={[4, 8, 6]} intensity={2.2} castShadow shadow-mapSize={[1024, 1024]} />
        <Environment resolution={256}>
          <Lightformer intensity={2} position={[0, 5, -6]} scale={[12, 4, 1]} color="#d6e8ff" />
          <Lightformer intensity={1.4} position={[-6, 3, 2]} scale={[4, 6, 1]} color="#ffd8a8" />
          <Lightformer intensity={1.2} position={[6, 2, 3]} scale={[4, 4, 1]} color="#a5f3fc" />
        </Environment>
        <PresentationControls global snap polar={[-0.06, 0.12]} azimuth={[-0.4, 0.4]}>
          <group position={[1.2, -1.1, 0]}>
            <mesh rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
              <circleGeometry args={[40, 64]} />
              <meshStandardMaterial color="#15243a" roughness={0.9} />
            </mesh>
            {items.map((item, i) => (
              <Pad key={item.id} item={item} x={start + i * gap} index={i} onOpen={() => onOpen(item.id)} motion={motion} anchors={anchors} />
            ))}
            {motion && <Sparkles count={36} scale={[14, 4, 6]} position={[0, 2.4, -1]} size={2} speed={0.25} opacity={0.35} color="#d6e8ff" />}
            <ContactShadows position={[0, 0.005, 0]} opacity={0.45} scale={18} blur={2.4} far={4} />
          </group>
        </PresentationControls>
      </Canvas>
      <LabelLayer anchors={anchors} labels={items.map((i) => ({ id: i.id, text: `${i.agent} · ${i.status}`, dot: TONE[i.tone] }))} />
    </div>
  );
}
