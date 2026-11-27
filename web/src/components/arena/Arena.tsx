"use client";

import { useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import {
  ContactShadows,
  Environment,
  Float,
  Lightformer,
  PresentationControls,
  RoundedBox,
  Sparkles,
} from "@react-three/drei";
import { ACESFilmicToneMapping, AdditiveBlending, CatmullRomCurve3, Color, SRGBColorSpace, ShaderMaterial, Vector3 } from "three";
import type { Group, Mesh } from "three";
import { Anchor, LabelLayer, type AnchorMap, type LabelSpec } from "./Labels";
import { Robot, type Mood } from "./Robot";

export type GateState = "none" | "locked" | "opening" | "open" | "breached" | "sealed";

export interface PodStep {
  label: string;
  runs: number;
  failures: number;
  outcome: "PASS" | "FAIL" | "INCONCLUSIVE";
  flags: string[];
  hasCulprit: boolean;
}

export interface ArenaProps {
  team: string;
  agentName: string;
  environment: string;
  steps: PodStep[];
  culprit: string | null;
  mood: Mood;
  gate: GateState;
  selected: number | null;
  onSelect: (i: number | null) => void;
  motion: boolean;
}

const OUTCOME_COLOR = { PASS: "#4ade80", FAIL: "#f87171", INCONCLUSIVE: "#fbbf24" } as const;
const GATE_COLOR: Record<GateState, string> = {
  none: "#64748b",
  locked: "#fbbf24",
  opening: "#7dd3fc",
  open: "#4ade80",
  breached: "#f87171",
  sealed: "#64748b",
};
const GATE_POS = new Vector3(3.0, 0, -1.6);
const PROD_POS = new Vector3(6.0, 0, -4.4);
const ROBOT_HOME = new Vector3(-0.5, 0, 0.5);

function podPosition(i: number, n: number): Vector3 {
  // an arc on the left of the stage, two rows when there are many steps
  const perRow = Math.min(n, 6);
  const row = Math.floor(i / perRow);
  const col = i % perRow;
  const x = -5.8 + col * 0.95 + row * 0.48;
  const z = -2.9 + row * 1.2 + Math.sin((col / Math.max(1, perRow - 1)) * Math.PI) * -0.45;
  return new Vector3(x, 0, z);
}

function Pod({
  step,
  index,
  position,
  active,
  selected,
  onSelect,
  motion,
  team,
  hovered,
  onHover,
  anchors,
}: {
  step: PodStep;
  index: number;
  position: Vector3;
  active: boolean;
  selected: boolean;
  onSelect: () => void;
  motion: boolean;
  team: string;
  hovered: boolean;
  onHover: (on: boolean) => void;
  anchors: AnchorMap;
}) {
  const group = useRef<Group>(null);
  const hover = hovered;
  const born = useRef<number | null>(null);
  const color = OUTCOME_COLOR[step.outcome];

  useFrame(({ clock }) => {
    if (!group.current) return;
    const t = clock.getElapsedTime();
    if (born.current === null) born.current = t + index * 0.07; // staggered entrance
    const k = motion ? Math.min(1, Math.max(0, (t - born.current) / 0.45)) : 1;
    const ease = 1 - Math.pow(1 - k, 3);
    const lift = (hover || selected ? 0.18 : 0) + (active && motion ? Math.sin(t * 3) * 0.04 : 0);
    group.current.position.y += (lift - group.current.position.y) * 0.15;
    const s = 0.9 + 0.1 * ease;
    group.current.scale.setScalar(s);
  });

  return (
    <group position={position}>
      <group
        ref={group}
        onPointerOver={(e) => {
          e.stopPropagation();
          onHover(true);
          document.body.style.cursor = "pointer";
        }}
        onPointerOut={() => {
          onHover(false);
          document.body.style.cursor = "";
        }}
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
      >
        {/* pedestal with a status ring */}
        <mesh position={[0, 0.06, 0]} receiveShadow castShadow>
          <cylinderGeometry args={[0.42, 0.46, 0.12, 40]} />
          <meshPhysicalMaterial color="#e8eaf2" roughness={0.4} clearcoat={0.5} />
        </mesh>
        <mesh position={[0, 0.125, 0]} rotation={[-Math.PI / 2, 0, 0]}>
          <ringGeometry args={[0.34, 0.41, 48]} />
          <meshStandardMaterial color={color} emissive={color} emissiveIntensity={2} toneMapped={false} />
        </mesh>
        {/* the glass sandbox */}
        <mesh position={[0, 0.72, 0]}>
          <capsuleGeometry args={[0.36, 0.62, 8, 32]} />
          <meshPhysicalMaterial
            color={color}
            transmission={0.92}
            thickness={0.25}
            roughness={0.06}
            ior={1.3}
            transparent
            opacity={0.95}
            attenuationColor={color}
            attenuationDistance={3}
          />
        </mesh>
        <group position={[0, 0.16, 0]}>
          <Robot team={team} mood={step.outcome === "FAIL" ? "sad" : "idle"} scale={0.42} motion={motion} seed={index} />
        </group>

        <Anchor id={`pod-${index}`} anchors={anchors} position={[0, 1.85, 0]} />
      </group>
    </group>
  );
}

/** The culprit token as the robot holds it in its raised right hand. */
function HeldToken({ motion }: { motion: boolean }) {
  const ref = useRef<Group>(null);
  useFrame((_, dt) => {
    if (ref.current && motion) ref.current.rotation.y += dt * 1.8;
  });
  return (
    <group ref={ref} position={[0, 0.06, 0]}>
      <mesh>
        <octahedronGeometry args={[0.17]} />
        <meshStandardMaterial color="#ef4444" emissive="#ef4444" emissiveIntensity={1.1} roughness={0.3} />
      </mesh>
      <pointLight color="#ef4444" intensity={2.5} distance={2.2} />
    </group>
  );
}

/** On approval the token leaves the hand, turns green and flies through the gate to Prod. */
function Token({ from, gate, motion, anchors }: { from: Vector3; gate: GateState; motion: boolean; anchors: AnchorMap }) {
  const ref = useRef<Group>(null);
  const progress = useRef(0);
  const curve = useMemo(
    () =>
      new CatmullRomCurve3([
        new Vector3(from.x + 0.93, 1.72, from.z),
        new Vector3(GATE_POS.x - 0.6, 2.6, GATE_POS.z + 0.6),
        new Vector3(GATE_POS.x, 1.4, GATE_POS.z),
        new Vector3(PROD_POS.x, 2.2, PROD_POS.z),
      ]),
    [from],
  );
  const flying = gate === "opening" || gate === "open";

  useFrame((_, dt) => {
    if (!ref.current) return;
    const target = flying ? 1 : 0;
    progress.current = motion
      ? progress.current + (target - progress.current) * Math.min(1, dt * 1.2)
      : target;
    ref.current.position.copy(curve.getPoint(progress.current));
    ref.current.rotation.y += motion ? dt * 1.8 : 0;
  });

  const color = "#22c55e";
  return (
    <group ref={ref}>
      <Float speed={motion ? 2 : 0} floatIntensity={0.4} rotationIntensity={0}>
        <mesh>
          <octahedronGeometry args={[0.2]} />
          <meshStandardMaterial color={color} emissive={color} emissiveIntensity={1.4} roughness={0.3} />
        </mesh>
        <pointLight color={color} intensity={3} distance={2.5} />
        <Anchor id="token" anchors={anchors} position={[0, 0.42, 0]} />
      </Float>
    </group>
  );
}

const FIELD_VERTEX = `
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

// An energy field: bright edges, fine moving scanlines and one sweeping band, added to the scene
// so whatever stands behind the gate shows through.
const FIELD_FRAGMENT = `
uniform vec3 uColor;
uniform float uTime;
uniform float uOpacity;
varying vec2 vUv;
void main() {
  float d = min(min(vUv.x, 1.0 - vUv.x), min(vUv.y, 1.0 - vUv.y));
  float rim = 1.0 - smoothstep(0.0, 0.07, d);
  float band = exp(-pow((fract(vUv.y - uTime * 0.28) - 0.5) * 12.0, 2.0));
  float lines = 0.5 + 0.5 * sin(vUv.y * 140.0 - uTime * 5.0);
  float a = uOpacity * (0.18 + rim * 0.95 + band * 0.7 + lines * 0.1);
  gl_FragColor = vec4(uColor * (0.7 + rim * 1.4 + band), a);
}
`;

function Gate({ state, motion }: { state: GateState; motion: boolean }) {
  const left = useRef<Mesh>(null);
  const right = useRef<Mesh>(null);
  const field = useRef<ShaderMaterial>(null);
  const uniforms = useMemo(() => ({ uColor: { value: new Color("#fbbf24") }, uTime: { value: 0 }, uOpacity: { value: 0 } }), []);
  const color = GATE_COLOR[state];
  const open = state === "opening" || state === "open";

  useFrame(({ clock }, dt) => {
    const t = clock.getElapsedTime();
    const k = motion ? Math.min(1, dt * 2.5) : 1;
    const slide = open ? 0.98 : 0.43;
    if (left.current) left.current.position.x += (-slide - left.current.position.x) * k;
    if (right.current) right.current.position.x += (slide - right.current.position.x) * k;
    const target = open ? 0 : state === "locked" ? 0.62 : state === "breached" ? 0.5 : 0.22;
    const f = field.current;
    if (f) {
      f.uniforms.uOpacity.value += (target - f.uniforms.uOpacity.value) * k;
      f.uniforms.uTime.value = motion ? t : 0;
      f.uniforms.uColor.value.set(color);
    }
  });

  return (
    <group position={GATE_POS} rotation={[0, -0.42, 0]}>
      {[-1.25, 1.25].map((x) => (
        <RoundedBox key={x} args={[0.38, 2.9, 0.5]} radius={0.12} position={[x, 1.45, 0]} castShadow receiveShadow>
          <meshPhysicalMaterial color="#e8eaf2" roughness={0.35} clearcoat={0.6} />
        </RoundedBox>
      ))}
      <RoundedBox args={[2.9, 0.34, 0.56]} radius={0.12} position={[0, 2.95, 0]} castShadow>
        <meshPhysicalMaterial color="#e8eaf2" roughness={0.35} clearcoat={0.6} />
      </RoundedBox>
      <mesh position={[0, 2.95, 0.29]}>
        <boxGeometry args={[1.6, 0.07, 0.02]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={1.8} toneMapped={false} />
      </mesh>
      <pointLight position={[0, 2.6, 0.6]} color={color} intensity={2.4} distance={3.2} />
      {/* doors */}
      <mesh ref={left} position={[-0.42, 1.3, 0]} castShadow>
        <boxGeometry args={[0.84, 2.5, 0.08]} />
        <meshPhysicalMaterial color="#e6edf8" roughness={0.42} transmission={0.6} thickness={0.2} clearcoat={0.6} transparent opacity={0.85} />
      </mesh>
      <mesh ref={right} position={[0.42, 1.3, 0]} castShadow>
        <boxGeometry args={[0.84, 2.5, 0.08]} />
        <meshPhysicalMaterial color="#e6edf8" roughness={0.42} transmission={0.6} thickness={0.2} clearcoat={0.6} transparent opacity={0.85} />
      </mesh>
      {/* energy field in front of the doors */}
      <mesh position={[0, 1.3, 0.1]}>
        <planeGeometry args={[2.1, 2.55]} />
        <shaderMaterial
          ref={field}
          vertexShader={FIELD_VERTEX}
          fragmentShader={FIELD_FRAGMENT}
          uniforms={uniforms}
          transparent
          depthWrite={false}
          blending={AdditiveBlending}
        />
      </mesh>
    </group>
  );
}

function Stage({ team, gate, motion, anchors }: { team: string; gate: GateState; motion: boolean; anchors: AnchorMap }) {
  const prodMood: Mood = gate === "open" ? "happy" : gate === "opening" ? "search" : "sad";
  return (
    <>
      <mesh rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
        <circleGeometry args={[40, 64]} />
        <meshStandardMaterial color="#15243a" roughness={0.9} />
      </mesh>
      {/* zone rings */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[-3.4, 0.01, -2.4]}>
        <ringGeometry args={[3.3, 3.34, 96]} />
        <meshBasicMaterial color="#3d7cc9" transparent opacity={0.5} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[PROD_POS.x, 0.01, PROD_POS.z]}>
        <ringGeometry args={[1.5, 1.54, 96]} />
        <meshBasicMaterial color={GATE_COLOR[gate === "none" ? "sealed" : gate]} transparent opacity={0.7} />
      </mesh>
      {/* production platform behind the gate */}
      <group position={PROD_POS}>
        <mesh position={[0, 0.1, 0]} receiveShadow castShadow>
          <cylinderGeometry args={[1.3, 1.4, 0.2, 64]} />
          <meshPhysicalMaterial color="#e8eaf2" roughness={0.4} clearcoat={0.5} />
        </mesh>
        <group position={[0, 0.2, 0]}>
          <Robot team={team} mood={prodMood} scale={0.95} motion={motion} seed={7} />
        </group>
        <Anchor id="prod" anchors={anchors} position={[0, 2.55, 0]} />
      </group>
      <Anchor id="sandbox" anchors={anchors} position={[-3.4, 2.35, -3.4]} />
    </>
  );
}

/** Narrow screens see a wider slice: the camera pulls back as the aspect ratio drops. */
function CameraRig() {
  useFrame(({ camera, size }) => {
    const aspect = size.width / Math.max(1, size.height);
    // keep the whole cast (pod arc to the Prod platform, about 7.4 units each side) in frame
    const dist = aspect >= 1.45 ? 13.2 : 6.3 / (Math.tan((17 * Math.PI) / 180) * aspect);
    camera.position.z = dist; // snap: a resize should frame the cast at once, not drift
    camera.position.y = 4.6 * (dist / 13.2) * 0.85 + 0.7;
    camera.lookAt(0.3, -0.2, -1.2);
  });
  return null;
}

function Scene(props: ArenaProps & { anchors: AnchorMap; hovered: number | null; onHover: (i: number | null) => void }) {
  const { steps, mood, gate, culprit, team, motion, selected, onSelect, anchors, hovered, onHover } = props;
  const robot = useRef<Group>(null);
  const searching = mood === "search";
  const flying = gate === "opening" || gate === "open";
  const lastPod = steps.length > 0 ? podPosition(steps.length - 1, steps.length) : ROBOT_HOME;
  const target = searching ? new Vector3(lastPod.x + 0.7, 0, lastPod.z + 1.1) : ROBOT_HOME;

  useFrame((_, dt) => {
    if (!robot.current) return;
    const k = motion ? Math.min(1, dt * 1.6) : 1;
    robot.current.position.lerp(target, k);
  });

  const keeperMood: Mood = gate === "open" || gate === "opening" ? "happy" : gate === "breached" ? "sad" : "idle";

  return (
    <>
      <CameraRig />
      <color attach="background" args={["#0e1a2d"]} />
      <fog attach="fog" args={["#0e1a2d", 16, 44]} />
      <ambientLight intensity={0.55} />
      <hemisphereLight args={["#d6e8ff", "#0b1424", 0.7]} />
      <directionalLight
        position={[5, 9, 6]}
        intensity={2.2}
        castShadow
        shadow-mapSize={[1024, 1024]}
        shadow-camera-left={-9}
        shadow-camera-right={9}
        shadow-camera-top={9}
        shadow-camera-bottom={-9}
      />
      <directionalLight position={[-5, 6, -9]} intensity={1.6} color="#9cc9ff" />
      <Environment resolution={256}>
        <Lightformer intensity={2} position={[0, 5, -6]} scale={[12, 4, 1]} color="#d6e8ff" />
        <Lightformer intensity={1.5} position={[-6, 3, 2]} scale={[4, 6, 1]} color="#ffd8a8" />
        <Lightformer intensity={1.2} position={[6, 2, 3]} scale={[4, 4, 1]} color="#a5f3fc" />
      </Environment>

      <PresentationControls
        global
        snap
        polar={[-0.08, 0.18]}
        azimuth={[-0.5, 0.5]}
        speed={1.2}
      >
        <group position={[-0.3, -1.0, 0]}>
          <Stage team={team} gate={gate} motion={motion} anchors={anchors} />
          {steps.map((s, i) => (
            <Pod
              key={i}
              step={s}
              index={i}
              position={podPosition(i, steps.length)}
              active={searching && i === steps.length - 1}
              selected={selected === i}
              onSelect={() => onSelect(selected === i ? null : i)}
              motion={motion}
              team={team}
              hovered={hovered === i}
              onHover={(on) => onHover(on ? i : hovered === i ? null : hovered)}
              anchors={anchors}
            />
          ))}
          <group ref={robot} position={ROBOT_HOME}>
            <Robot team={team} mood={mood} scale={1.15} motion={motion} hand={culprit && !flying ? <HeldToken motion={motion} /> : undefined} />
          </group>
          {culprit && !flying && (
            // the held token's label floats above the raised hand, over both robots' heads, never across a face
            <Anchor id="token" anchors={anchors} position={[ROBOT_HOME.x + 0.95, 2.72, ROBOT_HOME.z]} />
          )}
          {culprit && flying && <Token from={ROBOT_HOME} gate={gate} motion={motion} anchors={anchors} />}
          <Gate state={gate} motion={motion} />
          <group position={[GATE_POS.x - 1.75, 0, GATE_POS.z + 1.25]} rotation={[0, -0.35, 0]}>
            <Robot team="#3b82f6" mood={keeperMood} scale={1.3} motion={motion} shield seed={3} />
          </group>
          <Anchor id="keeper" anchors={anchors} position={[GATE_POS.x - 1.75, 3.6, GATE_POS.z + 1.25]} />
          {motion && <Sparkles count={40} scale={[16, 5, 8]} position={[0, 2.5, -1]} size={2} speed={0.25} opacity={0.35} color="#d6e8ff" />}
          {gate === "open" && (
            <Sparkles count={60} scale={[3, 3, 3]} position={[PROD_POS.x, 1.8, PROD_POS.z]} size={5} speed={motion ? 0.8 : 0} opacity={0.9} color="#4ade80" />
          )}
          <ContactShadows position={[0, 0.005, 0]} opacity={0.45} scale={22} blur={2.4} far={5} />
        </group>
      </PresentationControls>
    </>
  );
}

/** The investigation as a living 3D stage: sandbox pods, the agent, the culprit token, the gate. */
export default function Arena(props: ArenaProps) {
  const anchors = useMemo<AnchorMap>(() => new Map(), []);
  const [hovered, setHovered] = useState<number | null>(null);
  const flying = props.gate === "opening" || props.gate === "open";
  const keeper =
    props.gate === "open" ? "Change verified" : props.gate === "opening" ? "Writing the change" : props.gate === "locked" ? "Waiting for your approval" : props.gate === "breached" ? "Read-back failed" : "Gate closed";
  const prodDot = props.gate === "open" ? "#4ade80" : props.gate === "opening" ? "#7dd3fc" : "#f87171";
  const labels: LabelSpec[] = [
    { id: "sandbox", text: `${props.agentName} replays`, className: "tag-zone" },
    { id: "prod", text: `Flagsmith · ${props.environment}`, dot: prodDot, className: "tag-zone" },
    { id: "keeper", text: keeper, dot: GATE_COLOR[props.gate], className: "tag-zone" },
  ];
  if (props.culprit) labels.push({ id: "token", text: props.culprit, dot: flying ? "#22c55e" : "#ef4444", className: "tag-token" });
  for (const i of new Set([hovered, props.selected])) {
    const s = i === null ? undefined : props.steps[i];
    if (s) labels.push({ id: `pod-${i}`, text: `${s.label} · ${s.failures}/${s.runs} failed`, dot: OUTCOME_COLOR[s.outcome] });
  }
  return (
    <div className="arena">
      <Canvas
        shadows
        dpr={[1, 2]}
        camera={{ position: [0.4, 4.6, 13.2], fov: 34 }}
        gl={{ antialias: true, toneMapping: ACESFilmicToneMapping, outputColorSpace: SRGBColorSpace }}
        onPointerMissed={() => props.onSelect(null)}
        onCreated={({ camera }) => camera.lookAt(0.3, -0.2, -1.2)}
      >
        <Scene {...props} anchors={anchors} hovered={hovered} onHover={setHovered} />
      </Canvas>
      <LabelLayer anchors={anchors} labels={labels} />
    </div>
  );
}
