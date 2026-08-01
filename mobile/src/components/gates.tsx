/**
 * The two things a turn stops to ask.
 *
 * Both sit directly above the composer rather than in a modal, and that placement is the point:
 * the session is *waiting*, and a card in the flow says so while leaving the transcript readable
 * behind it. A modal would hide the very output the decision is about.
 */

import { Check, Hand, MessageCircleQuestion, X } from "lucide-react-native";
import { useState } from "react";
import { ScrollView, StyleSheet, TextInput, View } from "react-native";

import type { PendingPermission, PendingQuestion } from "../lib/transcript";
import { useTheme } from "../theme";
import { Button, Pill, Text } from "./ui";

export function PermissionGate({
  request, onAnswer,
}: {
  request: PendingPermission;
  onAnswer: (decision: "allow_once" | "deny") => void;
}) {
  const theme = useTheme();
  const detail = request.command || String(request.arguments.file_path ?? request.arguments.url ?? "");

  return (
    <View
      style={[
        styles.card,
        {
          backgroundColor: theme.colors.bgPanel,
          borderColor: theme.colors.borderEmphasized,
          borderRadius: theme.radii.md,
          padding: theme.space[3],
          gap: theme.space[2.5],
        },
        theme.shadow,
      ]}
    >
      <View style={[styles.heading, { gap: theme.space[2] }]}>
        <Hand size={15} color={theme.colors.yellowFg} />
        <Text variant="title" style={{ flex: 1 }}>Approval needed</Text>
        {request.risk ? <Pill label={request.risk} tone={request.risk === "high" ? "danger" : "warning"} /> : null}
      </View>

      {request.explanation ? <Text variant="small" tone="muted">{request.explanation}</Text> : null}

      {detail ? (
        <ScrollView
          style={{ maxHeight: 130 }}
          contentContainerStyle={{
            backgroundColor: theme.colors.bgSubtle,
            borderRadius: theme.radii.sm,
            padding: theme.space[2],
          }}
        >
          <Text variant="mono">{detail}</Text>
        </ScrollView>
      ) : (
        <Text variant="small" tone="muted">{request.toolName}</Text>
      )}

      <View style={{ flexDirection: "row", gap: theme.space[2] }}>
        <Button label="Deny" icon={X} onPress={() => onAnswer("deny")} style={{ flex: 1 }} />
        <Button
          label="Allow once" icon={Check} variant="solid" tone="accent"
          onPress={() => onAnswer("allow_once")} style={{ flex: 1 }}
        />
      </View>
    </View>
  );
}

interface QuestionShape {
  question?: string;
  header?: string;
  multiSelect?: boolean;
  options?: { label?: string; description?: string }[];
}

export function QuestionGate({
  request, onAnswer,
}: {
  request: PendingQuestion;
  onAnswer: (answers: unknown[], declined?: boolean) => void;
}) {
  const theme = useTheme();
  const questions = request.questions as QuestionShape[];
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<unknown[]>(() => questions.map(() => ""));
  const [typed, setTyped] = useState("");

  const current = questions[index];
  const last = index === questions.length - 1;

  const commit = (value: unknown) => {
    const next = [...answers];
    next[index] = value;
    setAnswers(next);
    setTyped("");
    if (last) onAnswer(next);
    else setIndex(index + 1);
  };

  if (!current) return null;

  return (
    <View
      style={[
        styles.card,
        {
          backgroundColor: theme.colors.bgPanel,
          borderColor: theme.colors.borderEmphasized,
          borderRadius: theme.radii.md,
          padding: theme.space[3],
          gap: theme.space[2.5],
        },
        theme.shadow,
      ]}
    >
      <View style={[styles.heading, { gap: theme.space[2] }]}>
        <MessageCircleQuestion size={15} color={theme.colors.purpleFg} />
        <Text variant="title" style={{ flex: 1 }}>{current.header || "A question"}</Text>
        {questions.length > 1 ? <Pill label={`${index + 1} of ${questions.length}`} /> : null}
      </View>

      <Text variant="body">{current.question ?? ""}</Text>

      <ScrollView style={{ maxHeight: 190 }} contentContainerStyle={{ gap: theme.space[1.5] }}>
        {(current.options ?? []).map((option, position) => (
          <Button
            key={`${option.label}-${position}`}
            label={option.label ?? ""}
            full
            onPress={() => commit(option.label ?? "")}
          />
        ))}
      </ScrollView>

      <TextInput
        value={typed}
        onChangeText={setTyped}
        placeholder="Or answer in your own words"
        placeholderTextColor={theme.colors.fgSubtle}
        style={[
          theme.text.body,
          {
            color: theme.colors.fg,
            backgroundColor: theme.colors.bgSubtle,
            borderRadius: theme.radii.md,
            paddingHorizontal: theme.space[2.5],
            height: theme.controlHeight,
          },
        ]}
        onSubmitEditing={() => typed.trim() && commit(typed.trim())}
      />

      <View style={{ flexDirection: "row", gap: theme.space[2] }}>
        <Button label="Dismiss" onPress={() => onAnswer([], true)} style={{ flex: 1 }} />
        <Button
          label={last ? "Answer" : "Next"} variant="solid" tone="accent" disabled={!typed.trim()}
          onPress={() => commit(typed.trim())} style={{ flex: 1 }}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderWidth: 1 },
  heading: { flexDirection: "row", alignItems: "center" },
});
