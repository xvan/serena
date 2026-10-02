Namespace TestProject.Services

    Public Class ConsoleGreeter
        Implements IGreeter

        Public Function FormatGreeting(name As String) As String Implements IGreeter.FormatGreeting
            Return $"Hello, {name}!"
        End Function

    End Class

End Namespace
