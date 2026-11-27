"use client";

import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import type { Group, Mesh } from "three";

export type Mood = "idle" | "search" | "alert" | "happy" | "sad";

const WHITE = "#f1f2f7";
const VISOR = "#0d1426";

const MOOD_LIGHT: Record<Mood, string> = {
  idle: "#7dd3fc",
  search: "#7dd3fc",
  alert: "#fbbf24",
  happy: "#4ade80",
  sad: "#f87171",
};

function Plastic({ color = WHITE, rough = 0.38 }: { color?: string; rough?: number }) {
  return <meshPhysicalMaterial color={color} roughness={rough} clearcoat={0.7} clearcoatRoughness={0.25} />;
}

/**
 * A small soft-plastic robot built from primitives: rounded body, glossy visor with glowing eyes,
 * team-coloured ear pods, belly plate and boots, and an antenna light that shows its mood.
 * `motion` is false under prefers-reduced-motion: the robot then holds a still pose.
 */
export function Robot({
  team,
  mood = "idle",
  scale = 1,
  motion = true,
  shield = false,
  seed = 0,
}: {
  team: string;
  mood?: Mood;
  scale?: number;
  motion?: boolean;
  shield?: boolean;
  seed?: number;
}) {
  const root = useRef<Group>(null);
  const head = useRef<Group>(null);
  const armL = useRef<Group>(null);
  const armR = useRef<Group>(null);
  const eyes = useRef<Group>(null);
  const antenna = useRef<Mesh>(null);
  const light = MOOD_LIGHT[mood];

  useFrame(({ clock }) => {
    if (!root.current || !head.current || !armL.current || !armR.current || !eyes.current) return;
    const t = clock.getElapsedTime() + seed * 1.7;
    if (!motion) return;
    const bob = mood === "happy" ? Math.abs(Math.sin(t * 5)) * 0.12 : Math.sin(t * 2) * 0.03;
    root.current.position.y = bob;
    // head: search scans left/right, alert tilts toward the token, sad droops
    const yaw = mood === "search" ? Math.sin(t * 1.6) * 0.6 : mood === "alert" ? -0.35 : Math.sin(t * 0.7) * 0.15;
    const pitch = mood === "sad" ? 0.35 : mood === "alert" ? -0.12 : Math.sin(t * 0.9) * 0.05;
    head.current.rotation.y += (yaw - head.current.rotation.y) * 0.08;
    head.current.rotation.x += (pitch - head.current.rotation.x) * 0.08;
    // arms: happy waves, alert raises the right hand (holding the token), otherwise swing gently
    const wave = mood === "happy" ? -2.4 + Math.sin(t * 9) * 0.4 : mood === "alert" ? -1.6 : Math.sin(t * 2) * 0.12;
    armR.current.rotation.z += (wave - armR.current.rotation.z) * 0.12;
    const left = mood === "happy" ? 2.4 - Math.sin(t * 9) * 0.4 : mood === "sad" ? 0.1 : -Math.sin(t * 2) * 0.12;
    armL.current.rotation.z += (left - armL.current.rotation.z) * 0.12;
    // blink every few seconds
    const blink = (t % 4.2) < 0.12 ? 0.1 : 1;
    eyes.current.scale.y += (blink - eyes.current.scale.y) * 0.5;
    if (antenna.current) antenna.current.scale.setScalar(1 + Math.sin(t * 4) * 0.12);
  });

  return (
    <group scale={scale}>
      <group ref={root}>
        {/* legs and boots */}
        {[-0.16, 0.16].map((x) => (
          <group key={x} position={[x, 0, 0]}>
            <mesh position={[0, 0.32, 0]} castShadow>
              <capsuleGeometry args={[0.085, 0.22, 6, 12]} />
              <Plastic />
            </mesh>
            <mesh position={[0, 0.09, 0.05]} castShadow>
              <boxGeometry args={[0.2, 0.14, 0.3]} />
              <Plastic color={team} rough={0.3} />
            </mesh>
          </group>
        ))}
        {/* body */}
        <mesh position={[0, 0.8, 0]} castShadow>
          <capsuleGeometry args={[0.3, 0.36, 8, 20]} />
          <Plastic />
        </mesh>
        <mesh position={[0, 0.82, 0.27]} castShadow>
          <boxGeometry args={[0.3, 0.2, 0.06]} />
          <Plastic color={team} rough={0.25} />
        </mesh>
        {shield && (
          <mesh position={[0, 0.84, 0.31]}>
            <circleGeometry args={[0.07, 24]} />
            <meshStandardMaterial color="#ffffff" emissive="#ffffff" emissiveIntensity={0.6} />
          </mesh>
        )}
        {/* arms pivot at the shoulder */}
        <group ref={armL} position={[-0.36, 1.02, 0]}>
          <mesh position={[0, -0.22, 0]} castShadow>
            <capsuleGeometry args={[0.075, 0.26, 6, 12]} />
            <Plastic />
          </mesh>
          <mesh position={[0, -0.44, 0]} castShadow>
            <sphereGeometry args={[0.09, 16, 16]} />
            <Plastic color={team} />
          </mesh>
        </group>
        <group ref={armR} position={[0.36, 1.02, 0]}>
          <mesh position={[0, -0.22, 0]} castShadow>
            <capsuleGeometry args={[0.075, 0.26, 6, 12]} />
            <Plastic />
          </mesh>
          <mesh position={[0, -0.44, 0]} castShadow>
            <sphereGeometry args={[0.09, 16, 16]} />
            <Plastic color={team} />
          </mesh>
        </group>
        {/* head */}
        <group ref={head} position={[0, 1.42, 0]}>
          <mesh castShadow>
            <sphereGeometry args={[0.36, 32, 32]} />
            <Plastic />
          </mesh>
          <mesh position={[0, 0, 0.12]} scale={[1, 0.78, 0.82]}>
            <sphereGeometry args={[0.3, 32, 32]} />
            <meshPhysicalMaterial color={VISOR} roughness={0.08} metalness={0.3} clearcoat={1} />
          </mesh>
          <group ref={eyes} position={[0, 0.03, 0.385]}>
            {[-0.1, 0.1].map((x) => (
              <mesh key={x} position={[x, 0, 0]} scale={[1, 1.3, 0.45]}>
                <sphereGeometry args={[0.058, 20, 20]} />
                <meshStandardMaterial color={light} emissive={light} emissiveIntensity={2.2} toneMapped={false} />
              </mesh>
            ))}
          </group>
          {[-1, 1].map((s) => (
            <mesh key={s} position={[0.36 * s, 0, 0]} rotation={[0, 0, Math.PI / 2]} castShadow>
              <cylinderGeometry args={[0.11, 0.11, 0.08, 24]} />
              <Plastic color={team} rough={0.3} />
            </mesh>
          ))}
          <mesh position={[0, 0.42, 0]}>
            <cylinderGeometry args={[0.012, 0.012, 0.16, 8]} />
            <Plastic color="#c9ccd8" />
          </mesh>
          <mesh ref={antenna} position={[0, 0.52, 0]}>
            <sphereGeometry args={[0.045, 16, 16]} />
            <meshStandardMaterial color={light} emissive={light} emissiveIntensity={2.5} toneMapped={false} />
          </mesh>
        </group>
      </group>
    </group>
  );
}
