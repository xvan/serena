namespace CoreLib;

/// <summary>
/// Formatting helpers consumed from the VB.NET project, to cover resolution across a
/// project reference that crosses the language boundary.
/// </summary>
public static class Formatter
{
    public static string Currency(decimal amount) => $"{amount:0.00} EUR";

    public static string Label(string name) => $"<<{name}>>";
}
